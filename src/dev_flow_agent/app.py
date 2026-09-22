"""FastAPI app: shared task router plus product create/artifact routes."""

from __future__ import annotations

import hashlib
import json

from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from agent_core.api import TaskServicePorts, create_task_router
from agent_core.identity import (
    ANSWER_GATE,
    AuthenticationError,
    AuthorizationError,
    IdentityResolver,
    Policy,
)
from agent_core.runtime import GateAlreadyAnswered, RuntimeStore

from agent_core.ui import humanize_wait

from dev_flow_agent.artifacts import UnknownArtifact, open_store, read_artifact
from dev_flow_agent.config import Settings
from dev_flow_agent.progress import FAILED_STATUS
from dev_flow_agent.db import TaskStore
from dev_flow_agent.tasks import TaskQueue
from dev_flow_agent.ui.render import STATIC_DIR, artifact_or_empty, markdown_html, render_template
from dev_flow_agent.workflow.stages import (
    pipeline_steps,
    stages,
    step_for_node,
    step_for_turn,
)


def _public_task(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "title": row["title"],
        "status": row["status"],
        "repo_url": row["repo_url"],
        "branch": row["branch"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


#: Where a repair task belongs in the strip, and what to call it.
REPAIRS = {
    "review": ("review", "fix review findings"),
    "enforcement": ("enforce", "fix test gate"),
}


def _static_version() -> str:
    """A digest of the served static files, for the asset URLs.

    StaticFiles sends no Cache-Control, so a browser is free to keep serving a
    script it already has without revalidating. A deployed fix then reaches the
    server and not the page -- which is how a stale `task.js` kept rendering
    progress from the wrong field and reloading from an event that never fires.
    The digest changes when a file does, so the URL changes with it.
    """
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    queue = TaskQueue(tasks, runtime)

    prefix = settings.root_path
    app = FastAPI(title="dev-flow-agent")
    app.state.settings = settings
    app.state.tasks = tasks
    app.state.runtime = runtime
    app.state.artifacts = artifacts
    app.state.queue = queue

    policy = Policy(allow_anonymous_gates=settings.allow_anonymous_gates)
    ports = TaskServicePorts(
        get_task=lambda task_id: (
            _public_task(row) if (row := tasks.get(task_id)) else None
        ),
        list_tasks=lambda limit=50, status=None: [
            _public_task(r) for r in tasks.list(limit=limit, status=status)
        ],
    )
    app.include_router(
        create_task_router(
            runtime,
            ports=ports,
            resolver=IdentityResolver(),
            policy=policy,
            inbox_path="/internal/gates",
            prefix=prefix,
        )
    )
    static_path = f"{prefix}/static" if prefix else "/static"
    app.mount(static_path, StaticFiles(directory=str(STATIC_DIR)), name="static")
    static_version = _static_version()
    pages = APIRouter()

    def _href(path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{prefix}{path}"

    def _page_ctx(**extra):
        ctx = {
            "allow_anonymous_gates": settings.allow_anonymous_gates,
            "root_path": prefix,
            "static_version": static_version,
        }
        ctx.update(extra)
        return ctx

    def _run_events(row, cap: int = 20_000):
        """Every event this run has logged, oldest first.

        Paged, because `events_since` answers with the *oldest* batch after a
        cursor. A single capped call returned the first thousand events of the
        run and nothing after them, so on a long build the progress panel, the
        pipeline marker and the document list all froze at the same moment and
        went on showing the beginning.

        One read serves all three: they were each scanning the same rows.
        """
        task_id = row["task_id"]
        cursor = int(row["run_watermark"] or 0)
        events = []
        while len(events) < cap:
            batch = runtime.events_since(task_ref=task_id, after_id=cursor, limit=1000)
            if not batch:
                break
            events.extend(batch)
            cursor = batch[-1]["id"]
            if len(batch) < 1000:
                break
        return events

    def _artifact(event):
        """The document a `document_written` event announces.

        The name is in the payload, not in the `stage` column -- that column
        carries the persist label, which is not always a stage or turn name.
        """
        if event["event_type"] != "document_written":
            return ""
        try:
            return str(json.loads(event["payload_json"] or "{}").get("artifact") or "")
        except ValueError:
            return ""

    def _written(events):
        """Which documents *this run* has written.

        Existence is the wrong question. After a retry the store still holds
        every document of the previous run, and answering from the files makes
        the pipeline claim a stage the new run has not reached.
        """
        names = {_artifact(event) for event in events}
        return {stage.artifact for stage in stages() if stage.artifact in names}

    def _step_of_document(artifact: str):
        """The step that writes ``artifact``."""
        stage = next((st for st in stages() if st.artifact == artifact), None)
        return _step_named(stage.turn_label) if stage is not None else None

    def _first_turn_step():
        """Where a run that has not reported anything yet must be.

        `prepare` has no turn and no panel, so naming it leaves the page with
        nothing selected and nothing to show.
        """
        return next(s for s in pipeline_steps() if s.turn_label)

    def _position(events):
        """Where the run is, from the last event that says anything about it.

        Chronological, not furthest-along. Progress says where work reported; a
        finished document says a stage is behind us. Taking whichever reached
        furthest was wrong as soon as the pipeline could loop: a run sent back
        to building still had `enforcement.md` written from the pass before, so
        the page placed it after the gate and opened a stage where nothing was
        happening.
        """
        steps = pipeline_steps()
        index = {step.node: n for n, step in enumerate(steps)}
        position = None
        for event in events:
            kind = event["event_type"]
            if kind == "agent_progress":
                step = step_for_turn(event["stage"] or "")
                if step is not None:
                    position = step
            elif kind == "document_written":
                done = _step_of_document(_artifact(event))
                if done is None:
                    continue
                after = [st for st in steps[index[done.node] + 1 :] if st.turn_label]
                position = after[0] if after else done
        return position

    def _current_step(row, gate_node, events):
        """Where the run is.

        A gate being asked is definite. Otherwise the last event that places
        the run decides -- so a stage that has just finished hands over to the
        next, and a run sent back to an earlier stage is shown there.
        """
        if row["status"] == "waiting_human" and gate_node:
            # ...unless the gate's node belongs to no step, in which case the
            # events place it rather than the page marking nothing.
            return _step_for_gate(gate_node) or _position(events)
        if row["status"] == "waiting_human":
            # Waiting, but on nothing anyone can answer. The events still know
            # where it got to, and a strip with no marker at all reads as "the
            # page is broken" rather than "this run is stuck".
            return _position(events)
        if row["status"] not in ("queued", "running"):
            return None
        return _position(events) or _first_turn_step()

    def _repairs(plan_rows):
        """Steps for the repair work a run gave itself.

        A run that loops back is not going backwards -- it is doing something
        it had not done before. Moving the marker back to `build` says the
        opposite, and hides that this is the second attempt.
        """
        made = []
        for row in plan_rows or ():
            after_label, name = REPAIRS.get(str(row["origin"] or "plan"), (None, None))
            if after_label is None:
                continue
            same = [m for m in made if m["after"] == after_label]
            made.append(
                {
                    "key": f"repair-{row['seq']}",
                    "label": f"{name} {len(same) + 1}" if same else name,
                    "after": after_label,
                    "seq": int(row["seq"]),
                    "status": row["status"],
                }
            )
        return made

    def _pipeline(current, selected, plan_rows=()):
        # A gate belongs to the stage it gates, so clicking "review spec" opens
        # the spec -- the document being approved and the form approving it are
        # one place, not two steps apart.
        gated = {stage.gate: stage.turn_label for stage in stages() if stage.gate}
        repairs = _repairs(plan_rows)
        running = next((r for r in repairs if r["status"] == "running"), None)
        rows = []
        for step in pipeline_steps():
            selects = step.turn_label or gated.get(step.node, "")
            rows.append(
                {
                    "name": step.label,
                    # A running repair is where the run is, not the build step
                    # that happens to be executing it.
                    "current": (
                        current is not None
                        and step.node == current.node
                        and running is None
                    ),
                    "stage": step.turn_label,
                    "selects": selects,
                    "selected": bool(selects) and selects == selected,
                }
            )
            for repair in repairs:
                if repair["after"] != step.turn_label:
                    continue
                rows.append(
                    {
                        "name": repair["label"],
                        "current": running is not None and running["seq"] == repair["seq"],
                        "stage": "",
                        "selects": "build",
                        "selected": False,
                        "repair": repair["status"],
                    }
                )
        return rows

    def _running_step(events):
        """The step whose turn last reported progress, if any.

        Only progress events count. A `gate_opened` carries its gate's node
        name in the same column -- `review_plan` -- which the axis-prefix rule
        reads as the review step, so scanning every event type marked a
        building run as reviewing.
        """
        latest = None
        for event in events:
            if event["event_type"] != "agent_progress":
                continue
            step = step_for_turn(event["stage"] or "")
            if step is not None:
                latest = step
        return latest

    def _review_axes(events):
        """The review's agents and where each one is.

        Six of them run at once and write into one log, where their lines
        interleave into something no one can follow. This is the same answer
        the task tree gives for the build: which agent, and how far.
        """
        axes = {}
        for event in events:
            if event["event_type"] != "review_axis":
                continue
            try:
                payload = json.loads(event["payload_json"] or "{}")
            except ValueError:
                continue
            name = str(payload.get("name") or "")
            if not name:
                continue
            axes.setdefault(name, {"name": name, "order": len(axes)})
            axes[name].update(
                {
                    "title": payload.get("title") or name,
                    "status": payload.get("status") or "pending",
                    "verdict": payload.get("verdict") or "",
                }
            )
        return sorted(axes.values(), key=lambda a: a["order"])

    def _progress(events, limit: int = 300, under: str = ""):
        """This run's progress so far, for the page to render.

        The live view was built entirely in the browser, so it existed only
        until the next navigation -- opening a stage from the strip is an
        ordinary link, and it wiped the panel. The stream still carries what
        happens next; what it cannot carry is what already happened, because it
        starts at the id the page was rendered with.
        """
        rows = []
        for event in events:
            if event["event_type"] != "agent_progress":
                continue
            try:
                payload = json.loads(event["payload_json"] or "{}")
            except ValueError:
                payload = {}
            # The panel is one stage, so repeating its name on every line is
            # noise. A fanned-out branch keeps it: which agent said this is the
            # whole point of showing six of them together.
            stage = event["stage"] if event["stage"] != under else ""
            parts = [p for p in (stage, payload.get("tool")) if p]
            parts.append(payload.get("detail") or event["message"] or "progress")
            if payload.get("status") == FAILED_STATUS:
                # Colour alone does not survive a screenshot, a copy-paste, or
                # a reader who does not know the convention.
                parts.append("failed")
            rows.append(
                {
                    "at": event["created_at"],
                    "lane": event["stage"] or "",
                    "text": " · ".join(str(p) for p in parts),
                    "error": event["severity"] == "error",
                }
            )
        return rows[-limit:]

    def _lanes(events, step, axes):
        """One stream per agent when a stage runs several at once.

        Six reviewers writing into one list is six voices in one transcript:
        every line has to be read to find out whose it is, and the interleaving
        carries no meaning. Each gets its own lane, in the order they were
        announced.
        """
        rows = _progress(_stage_events(events, step), under=step.turn_label if step else "")
        if step is None or not step.fans_out:
            return [{"key": "", "label": "", "status": "", "rows": rows}]

        titles = {f"{step.turn_label}_{a['name']}": a for a in axes}
        lanes = {
            key: {"key": key, "label": a["title"], "status": a["status"], "rows": []}
            for key, a in titles.items()
        }
        for row in rows:
            lane = lanes.setdefault(
                row["lane"],
                {"key": row["lane"], "label": row["lane"], "status": "", "rows": []},
            )
            lane["rows"].append(row)
        return list(lanes.values())

    def _step_named(name: str):
        """The step a `?stage=` value selects, if any."""
        if not name:
            return None
        wanted = str(name).replace(" ", "_")
        return next((s for s in pipeline_steps() if s.turn_label == wanted), None)

    def _step_for_gate(node: str):
        """The panel that answers a gate: the stage the gate closes.

        A gate is a step in the strip but not a place with content of its own --
        the document being approved, the critique of it and the form are all
        the stage's. An escalation gate folds into the work that escalated.
        """
        stage = next((s for s in stages() if s.gate == node), None)
        if stage is not None:
            return _step_named(stage.turn_label)
        return step_for_node(node)

    def _stage_of(event):
        """Which step an event belongs to, whatever kind it is."""
        kind = event["event_type"]
        if kind == "gate_opened":
            return step_for_node(event["stage"] or "")
        if event["stage"]:
            by_turn = step_for_turn(event["stage"])
            if by_turn is not None:
                return by_turn
            # A document announces itself under its persist label ("auto design
            # review"), which is a stage label rather than a turn label.
            stage = next((s for s in stages() if s.label == event["stage"]), None)
            if stage is not None:
                return _step_named(stage.turn_label)
        return None

    def _stage_events(events, step):
        """This step's own events. An event belonging to no step belongs to the
        run rather than to any one part of it, and is not shown here."""
        if step is None:
            return []
        owned = []
        for event in events:
            owner = _stage_of(event)
            if owner is not None and owner.node == step.node:
                owned.append(event)
        return owned

    def _repo_for(text: str) -> str:
        """A repository URL or filesystem path.

        A bare name is refused here, while someone is still looking at the
        form, rather than later when the clone has nothing to fetch.
        """
        from dev_flow_agent.workflow.repos import looks_like_repo

        value = (text or "").strip()
        if looks_like_repo(value):
            return value
        raise HTTPException(status_code=422, detail="a repository URL or path is required")

    @pages.get("/", response_class=HTMLResponse)
    def home():
        return render_template("home.html", **_page_ctx(tasks=tasks.list(), error=None))

    @pages.post("/")
    async def home_create(
        title: str = Form(...),
        request: str = Form(...),
        repo_url: str = Form(...),
        branch: str = Form("main"),
    ):
        row, _ = tasks.create(
            title=title.strip(),
            request=request.strip(),
            repo_url=_repo_for(repo_url),
            branch=branch.strip() or "main",
            created_by=None,
            idempotency_key=None,
        )
        return RedirectResponse(_href(f"/tasks/{row['task_id']}"), status_code=303)

    @pages.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_page(task_id: str, stage: str = ""):
        row = tasks.get(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {task_id}")
        gate = next((g for g in runtime.pending_gates(limit=500) if g.task_ref == task_id), None)
        events = _run_events(row)

        # Which step the page is showing: the one asked for, else the one the
        # run is on. Everything below is that step's own work -- the single
        # long page mixed six stages of events, five documents and two trees,
        # and a reader had to know which lines belonged to what.
        gate_step = _step_for_gate(gate.node) if gate else None
        current = _current_step(row, gate.node if gate else None, events)
        # What a reader most likely came for: where the run is, else the last
        # thing it produced. A finished run opening on "prepare" is an empty
        # page, and a marker pointing somewhere the panel is not is a lie.
        written = _written(events)
        furthest = [s for s in stages() if s.artifact in written]
        last_written = _step_named(furthest[-1].turn_label) if furthest else None
        # The marker and the panel answer different questions -- where the run
        # is, and what you are looking at -- and the strip shows both: the step
        # is marked current, the link is marked selected. Opening on the last
        # thing produced beats opening on a stage that has not started.
        default = gate_step or _running_step(events) or last_written or current
        default = default or _first_turn_step()
        selected = _step_named(stage) or default
        selected_key = selected.turn_label if selected else ""

        stage_obj = next((s for s in stages() if s.turn_label == selected_key), None)
        review_of = None
        if stage_obj is not None and stage_obj.is_review:
            review_of = next((s for s in stages() if s.label == stage_obj.reviews), None)

        # The document this step is about. A critique step is asked about the
        # document it reviewed, not about itself.
        document = review_of or stage_obj
        name = document.artifact if document is not None else ""
        # Run-scoped, so a retry does not show the previous run's document as
        # though this one had written it -- unless a gate is asking about it,
        # in which case the reviewer must be able to read what they approve.
        on_gate = (
            gate_step is not None and selected is not None and gate_step.node == selected.node
        )
        mine = document is not None and (document.artifact in written or on_gate)
        body = artifact_or_empty(artifacts, task_id, name) if mine else ""

        critique = ""
        if stage_obj is not None and stage_obj.is_review:
            critique_body = artifact_or_empty(artifacts, task_id, stage_obj.artifact)
            critique = markdown_html(critique_body) if critique_body else ""

        return render_template(
            "task.html",
            **_page_ctx(
                task=_public_task(row),
                pipeline=_pipeline(current, selected_key, tasks.plan_tasks(task_id)),
                selected=selected_key,
                selected_label=selected.label if selected else "",
                selected_fans_out=bool(selected and selected.fans_out),
                # Whether the reader asked for this stage or is just watching
                # the run. Someone reading the spec while the build runs should
                # keep reading it; someone who opened the task should follow.
                pinned=bool(stage),
                artifact_name=name,
                artifact_html=markdown_html(body) if body else "",
                review_html=critique,
                waiting=(
                    row["status"] == "waiting_human"
                    and gate_step is not None
                    and selected is not None
                    and gate_step.node == selected.node
                ),
                gate=gate,
                lanes=_lanes(events, selected, _review_axes(events) if selected_key == "review" else []),
                plan_tasks=tasks.plan_tasks(task_id) if selected_key == "build" else [],
                review_axes=_review_axes(events) if selected_key == "review" else [],
                after_id=runtime.latest_event_id(task_ref=task_id),
            ),
        )

    @pages.post("/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        try:
            queue.retry(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(_href(f"/tasks/{task_id}"), status_code=303)

    @pages.post("/tasks/{task_id}/fail")
    def fail_task(task_id: str):
        try:
            queue.fail(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(_href(f"/tasks/{task_id}"), status_code=303)

    @pages.post("/gates/{gate_id}/answer")
    async def answer_gate(gate_id: str, request: Request):
        resolver = IdentityResolver()
        try:
            principal = resolver.resolve(dict(request.headers))
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        try:
            actor = policy.authorize(principal, ANSWER_GATE)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        form = await request.form()
        payload = {k: v for k, v in form.items() if k != "gate_id"}
        try:
            gate = runtime.answer_gate(
                gate_id=gate_id, response=payload, principal=actor, policy=policy
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GateAlreadyAnswered as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(_href(f"/tasks/{gate.task_ref}"), status_code=303)

    @pages.get("/gates", response_class=HTMLResponse)
    def gates_page():
        items = []
        for g in runtime.pending_gates():
            items.append(
                {
                    "task_ref": g.task_ref,
                    "node": g.node,
                    "waited": humanize_wait(g.requested_at),
                }
            )
        return render_template("gates.html", **_page_ctx(gates=items))

    @pages.post("/api/v1/tasks")
    async def create_task(
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError as exc:
            # A malformed body is a caller's mistake, not a server fault: this
            # raised out of the handler and answered 500.
            raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        title = str(body.get("title") or "").strip()
        request_text = str(body.get("request") or "").strip()
        repo_url = str(body.get("repo_url") or "").strip()
        branch = str(body.get("branch") or "").strip()
        if not title or not request_text or not repo_url or not branch:
            raise HTTPException(status_code=422, detail="title, request, repo_url, branch are required")
        try:
            row, created = tasks.create(
                title=title,
                request=request_text,
                repo_url=_repo_for(repo_url),
                branch=branch,
                created_by=None,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(_public_task(row), status_code=201 if created else 200)

    @pages.get("/api/v1/tasks/{task_id}/artifacts/{name}")
    def get_artifact(task_id: str, name: str) -> Response:
        if tasks.get(task_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {task_id}")
        try:
            data = read_artifact(artifacts, task_id, name)
        except UnknownArtifact:
            raise HTTPException(status_code=404, detail=f"unknown artifact: {name}")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"no artifact {name}")
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=data, media_type="text/markdown; charset=utf-8")

    app.include_router(pages, prefix=prefix)
    return app
