"""Home, task, and gates pages."""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app

    return TestClient(create_app())


def test_empty_home_has_copy_and_form(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "No runs yet" in res.text
    assert "<form" in res.text
    assert 'name="title"' in res.text


def test_form_create_redirects_to_task(client):
    res = client.post(
        "/",
        data={
            "title": "SSE",
            "request": "Watch it",
            "repo_url": "/tmp/repo",
            "branch": "main",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/tasks/")
    page = client.get(res.headers["location"])
    assert page.status_code == 200
    assert "SSE" in page.text


def test_script_in_artifact_is_text(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

    app = create_app()
    created = TestClient(app).post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    app.state.artifacts.write_text(
        f"{created['task_id']}/triage.md",
        "<script>alert(1)</script>\n# Hi",
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    app.state.runtime.append_event(
        task_ref=created["task_id"],
        event_type="document_written",
        message="wrote triage.md",
        payload={"artifact": "triage.md"},
    )
    html = TestClient(app).get(f"/tasks/{created['task_id']}").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html or "alert(1)" in html and "<script>" not in html


def test_gates_list_has_no_approve_button(client):
    res = client.get("/gates")
    assert res.status_code == 200
    assert "Approve" not in res.text
    assert "Nothing waiting" in res.text


def test_waiting_task_shows_gate_form(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app

    app = create_app()
    http = TestClient(app)
    created = http.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status='waiting_human' WHERE task_id=?",
        (created["task_id"],),
    )
    app.state.tasks._conn.commit()
    app.state.runtime.open_gate(
        task_ref=created["task_id"], node="review_triage", kind="input", prompt={}
    )
    html = http.get(f"/tasks/{created['task_id']}").text
    assert 'name="decision"' in html
    assert 'value="approve"' in html
    assert 'value="reject"' in html
    assert "Request changes" in html


def test_cli_help():
    from dev_flow_agent.cli import main

    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0


def test_anonymous_gates_banner(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("DFA_ALLOW_ANONYMOUS_GATES", "true")
    from dev_flow_agent.app import create_app

    html = TestClient(create_app()).get("/").text
    assert "anonymous gate answers" in html.lower()


def test_healthcheck_ok(client):
    res = client.get("/healthcheck.html")
    assert res.status_code == 200
    assert "ok" in res.text


def test_root_path_prefixes_pages_api_and_health(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("DFA_ROOT_PATH", "/dev-flow-agent")
    from dev_flow_agent.app import create_app

    http = TestClient(create_app())
    assert http.get("/healthcheck.html").status_code == 404
    health = http.get("/dev-flow-agent/healthcheck.html")
    assert health.status_code == 200
    assert "ok" in health.text
    home = http.get("/dev-flow-agent/")
    assert home.status_code == 200
    assert 'href="/dev-flow-agent/"' in home.text
    assert 'href="/dev-flow-agent/static/app.css?v=' in home.text
    created = http.post(
        "/dev-flow-agent/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
        follow_redirects=False,
    )
    assert created.status_code == 201
    form = http.post(
        "/dev-flow-agent/",
        data={
            "title": "SSE",
            "request": "Watch it",
            "repo_url": "/tmp/repo",
            "branch": "main",
        },
        follow_redirects=False,
    )
    assert form.status_code == 303
    assert form.headers["location"].startswith("/dev-flow-agent/tasks/")
    page = http.get(form.headers["location"])
    assert page.status_code == 200
    assert 'data-root="/dev-flow-agent"' in page.text


def test_failed_task_page_has_retry_and_post_requeues(client):
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]
    page = client.get(f"/tasks/{task_id}")
    assert "Retry" not in page.text
    app = client.app
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status='failed' WHERE task_id=?", (task_id,)
    )
    app.state.tasks._conn.commit()
    html = client.get(f"/tasks/{task_id}").text
    assert "Retry" in html
    assert f'action="/tasks/{task_id}/retry"' in html
    res = client.post(f"/tasks/{task_id}/retry", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == f"/tasks/{task_id}"
    assert app.state.tasks.get(task_id)["status"] == "queued"


def test_retry_unknown_or_not_failed_is_rejected(client):
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    assert client.post(f"/tasks/{created['task_id']}/retry", follow_redirects=False).status_code == 409
    assert client.post("/tasks/missing/retry", follow_redirects=False).status_code == 404


def test_queued_task_page_has_fail_and_post_marks_failed(client):
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]
    html = client.get(f"/tasks/{task_id}").text
    assert "Fail" in html
    assert f'action="/tasks/{task_id}/fail"' in html
    assert "Retry" not in html
    res = client.post(f"/tasks/{task_id}/fail", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == f"/tasks/{task_id}"
    page = client.get(f"/tasks/{task_id}")
    assert client.app.state.tasks.get(task_id)["status"] == "failed"
    assert "Retry" in page.text
    assert "Fail" not in page.text


def test_fail_unknown_or_already_failed_is_rejected(client):
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    client.post(f"/tasks/{created['task_id']}/fail", follow_redirects=False)
    assert client.post(f"/tasks/{created['task_id']}/fail", follow_redirects=False).status_code == 409
    assert client.post("/tasks/missing/fail", follow_redirects=False).status_code == 404


def _task_with_docs(tmp_path, *documents):
    from dev_flow_agent.app import create_app
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

    app = create_app()
    created = TestClient(app).post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    for name in documents:
        app.state.artifacts.write_text(
            f"{created['task_id']}/{name}",
            f"# {name}\nbody of {name}\n",
            max_bytes=MAX_ARTIFACT_BYTES,
        )
        # persist_doc writes the file and announces it; the page reads the
        # announcement, so a helper that only writes the file is not the
        # product's behaviour.
        app.state.runtime.append_event(
            task_ref=created["task_id"],
            event_type="document_written",
            message=f"wrote {name}",
            payload={"artifact": name},
        )
    return app, created["task_id"]


def test_the_strip_shows_the_whole_pipeline(tmp_path, monkeypatch):
    """Built from the flow's turns, so the stages that write code appear too.
    A strip built from documents had nothing to point at for the whole
    unattended half of a run."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.workflow.stages import pipeline_steps

    app, task_id = _task_with_docs(tmp_path)
    html = TestClient(app).get(f"/tasks/{task_id}").text
    for step in pipeline_steps():
        assert f">{step.label}</a>" in html or f">{step.label}</li>" in html, step.label
    for label in ("build", "review", "simplify", "review plan"):
        assert f">{label}</a>" in html


def test_the_page_opens_on_the_furthest_work_and_links_every_stage(tmp_path, monkeypatch):
    """One stage at a time: the page opens where the run got to, and the strip
    is how a reader reaches the others."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md", "spec.md", "design.md")
    html = TestClient(app).get(f"/tasks/{task_id}").text

    assert "body of design.md" in html
    assert "body of triage.md" not in html
    assert f"/tasks/{task_id}?stage=triage" in html
    assert f"/tasks/{task_id}?stage=build" in html


def test_a_stage_shows_its_own_document(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md", "spec.md")
    html = TestClient(app).get(f"/tasks/{task_id}?stage=triage").text
    assert "body of triage.md" in html
    assert "body of spec.md" not in html


def test_a_stage_shows_only_its_own_activity(tmp_path, monkeypatch):
    """Six stages of events in one list was the mess: a line belongs to the
    stage that produced it."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    _progress_event(app, task_id, message="reading", detail="in the build", stage="build")
    _progress_event(app, task_id, message="writing", detail="in the spec", stage="spec")
    client = TestClient(app)

    build = client.get(f"/tasks/{task_id}?stage=build").text
    assert "in the build" in build and "in the spec" not in build

    spec = client.get(f"/tasks/{task_id}?stage=spec").text
    assert "in the spec" in spec and "in the build" not in spec


def test_a_fanned_out_axis_files_under_the_review_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    _progress_event(app, task_id, message="auditing", detail="by security", stage="review_security")
    html = TestClient(app).get(f"/tasks/{task_id}?stage=review").text
    assert "by security" in html


def test_a_stage_with_no_document_yet_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    html = TestClient(app).get(f"/tasks/{task_id}?stage=plan").text
    assert "plan.md" in html
    assert "Not written yet." in html


def test_progress_events_reach_the_sse_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app
    from dev_flow_agent.progress import turn_progress_port

    app = create_app()
    client = TestClient(app)
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]

    class _Request:
        name = "write_intent"
        progress_detail = "public_detail"

    from agent_core.harness.registry import TurnProgress

    port = turn_progress_port(app.state.runtime, task_ref=task_id)
    publish = port.callback(request=_Request(), session_id="s1")
    publish(TurnProgress(kind="tool", message="read", tool="read", detail="src/app.py"))
    port.flush()
    # Terminal event, so the stream closes instead of polling to its cap.
    app.state.runtime.append_event(
        task_ref=task_id, event_type="task_completed", message="task completed"
    )

    with client.stream("GET", f"/api/v1/tasks/{task_id}/events") as res:
        body = "".join(res.iter_text())
    assert "event: agent_progress" in body
    assert "src/app.py" in body
    assert "event: task_completed" in body


def test_progress_section_has_an_empty_state(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert 'id="progress-empty"' in html
    assert "data-gate-id" in html


def test_stream_starts_after_a_previous_runs_terminal_event(tmp_path, monkeypatch):
    """A retried task keeps its old `task_failed`. Tailing from 0 would end the
    new stream on its first frame and reload the page in a loop."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app

    app = create_app()
    client = TestClient(app)
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]
    stale = app.state.runtime.append_event(
        task_ref=task_id, event_type="task_failed", message="a previous run"
    )

    html = client.get(f"/tasks/{task_id}").text
    assert f'data-after-id="{stale}"' in html
    # The cursor the page hands the stream skips that event; tailing from 0
    # would deliver it and terminate.
    assert app.state.runtime.events_since(task_ref=task_id, after_id=stale) == []
    assert [r["id"] for r in app.state.runtime.events_since(task_ref=task_id)] == [stale]


def test_gate_page_shows_the_automated_review(tmp_path, monkeypatch):
    """The critique is what the reviewer reads first, so it is on the page and
    not only in the gate payload."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

    app = create_app()
    client = TestClient(app)
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]
    for name, body in (
        ("design.md", "# Design\nthe design\n"),
        ("design-review.md", "# Review\nthe queue is the wrong layer\n"),
    ):
        app.state.artifacts.write_text(
            f"{task_id}/{name}", body, max_bytes=MAX_ARTIFACT_BYTES
        )
    app.state.runtime.open_gate(
        task_ref=task_id,
        node="review_design",
        kind="input",
        prompt={"title": "x"},
        thread_id="t",
    )
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'waiting_human' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()

    html = client.get(f"/tasks/{task_id}").text
    assert "Automated review" in html
    assert "the queue is the wrong layer" in html
    assert 'value="approve"' in html


def test_static_urls_carry_a_content_version(client):
    """A deployed fix must reach the page, not just the server."""
    html = client.get("/").text
    assert "/static/app.css?v=" in html


def test_the_version_changes_when_a_static_file_changes(tmp_path, monkeypatch):
    import dev_flow_agent.app as app_module

    first = app_module._static_version()
    original = (app_module.STATIC_DIR / "task.js").read_bytes()
    try:
        (app_module.STATIC_DIR / "task.js").write_bytes(original + b"\n// changed\n")
        assert app_module._static_version() != first
    finally:
        (app_module.STATIC_DIR / "task.js").write_bytes(original)
    assert app_module._static_version() == first


def test_the_task_page_versions_its_script(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert "/static/task.js?v=" in html


def test_retry_resets_the_pipeline_to_the_stage_the_new_run_is_at(tmp_path, monkeypatch):
    """A retry starts a new run at the first stage. The previous run's
    documents are all still in the store, and reading the pipeline off them
    reported a stage the new run had not reached."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md", "spec.md", "design.md")
    client = TestClient(app)

    before = client.get(f"/tasks/{task_id}").text
    assert "body of design.md" in before

    app.state.tasks.fail(task_id)
    app.state.queue.retry(task_id)

    after = client.get(f"/tasks/{task_id}").text
    # Nothing from the old run is claimed as this one's work...
    assert "body of design.md" not in after
    assert "body of triage.md" not in after
    assert "Not written yet." in after
    # ...and both the marker and the panel point at where it restarts.
    assert 'data-stage="triage"' in after
    assert 'aria-current="step"' in after


def test_retry_keeps_serving_the_stored_documents_over_the_api(tmp_path, monkeypatch):
    """Resetting the view must not lose the documents themselves."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md", "spec.md")
    client = TestClient(app)
    app.state.tasks.fail(task_id)
    app.state.queue.retry(task_id)

    res = client.get(f"/api/v1/tasks/{task_id}/artifacts/spec.md")
    assert res.status_code == 200
    assert "body of spec.md" in res.text


def _progress_event(app, task_id, *, message, detail=None, stage="spec", tool=None, severity="info"):
    return app.state.runtime.append_event(
        task_ref=task_id,
        event_type="agent_progress",
        severity=severity,
        stage=stage,
        message=message,
        payload={"tool": tool, "detail": detail},
    )


def test_progress_survives_opening_another_stage(tmp_path, monkeypatch):
    """The strip is ordinary links, so the page reloads. Progress lived only in
    the browser and the panel came back empty."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md", "spec.md")
    _progress_event(app, task_id, message="Read project context", detail="src/app.py", tool="read")
    client = TestClient(app)

    for url in (f"/tasks/{task_id}?stage=spec", f"/tasks/{task_id}?stage=spec&x=1"):
        html = client.get(url).text
        assert "src/app.py" in html
        # The panel is one stage, so its name is not repeated on every line.
        assert "read · src/app.py" in html
        assert "spec · read" not in html


def test_rendered_progress_hides_the_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    client = TestClient(app)
    assert 'id="progress-empty" class="muted">' in client.get(f"/tasks/{task_id}").text

    _progress_event(app, task_id, message="Agent update", detail="writing")
    assert 'id="progress-empty" class="muted" hidden>' in client.get(f"/tasks/{task_id}").text


def test_rendered_progress_carries_a_timestamp_and_is_run_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    _progress_event(app, task_id, message="from the old run", detail="old")
    client = TestClient(app)
    assert "<time class=\"progress-time\"" in client.get(f"/tasks/{task_id}").text

    app.state.tasks.fail(task_id)
    app.state.queue.retry(task_id)
    after = client.get(f"/tasks/{task_id}").text
    assert "from the old run" not in after
    lanes = after.split('id="progress-lanes"')[1]
    assert "old" not in lanes.split("</section>")[0]


def _open_gate(app, task_id, node):
    app.state.runtime.open_gate(
        task_ref=task_id, node=node, kind="input", prompt={"title": "x"}, thread_id="t"
    )
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'waiting_human' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()


def test_the_gate_shows_its_document_even_with_nothing_recorded(tmp_path, monkeypatch):
    """A task whose documents predate `document_written` still has to be
    reviewable: the reviewer cannot approve a spec they cannot read."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

    app = create_app()
    client = TestClient(app)
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]
    # Written, but never announced -- exactly the state on beta.
    app.state.artifacts.write_text(
        f"{task_id}/spec.md", "# Spec\nthe spec body\n", max_bytes=MAX_ARTIFACT_BYTES
    )
    _open_gate(app, task_id, "review_spec")

    html = client.get(f"/tasks/{task_id}").text
    assert "the spec body" in html
    assert "spec.md" in html
    assert 'value="approve"' in html


def test_the_design_gate_shows_the_design_not_the_critique_as_the_document(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

    app = create_app()
    client = TestClient(app)
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]
    for name, body in (
        ("design.md", "# Design\nthe design body\n"),
        ("design-review.md", "# Review\nthe critique body\n"),
    ):
        app.state.artifacts.write_text(f"{task_id}/{name}", body, max_bytes=MAX_ARTIFACT_BYTES)
    _open_gate(app, task_id, "review_design")

    html = client.get(f"/tasks/{task_id}").text
    # The gate is closed by the critique stage, but the document under review
    # is the design; the critique is shown as the automated review.
    assert "the design body" in html
    assert "the critique body" in html
    assert "Automated review" in html


def test_the_page_records_the_status_it_was_rendered_with(tmp_path, monkeypatch):
    """The script needs it to tell a stale view from a current one, so that a
    page already showing a running task does not reload on every claim."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert 'data-status="queued"' in html

    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'running' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()
    assert 'data-status="running"' in TestClient(app).get(f"/tasks/{task_id}").text


def test_the_strip_follows_the_turn_that_is_running(tmp_path, monkeypatch):
    """A stage has written nothing until it finishes, so documents cannot say
    what is executing. The turn's own progress can."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'running' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()
    client = TestClient(app)

    _progress_event(app, task_id, message="Read project context", stage="build")
    assert 'aria-current="step" data-stage="build"' in client.get(f"/tasks/{task_id}").text

    # A fanned-out axis belongs to the review step...
    _progress_event(app, task_id, message="Reviewing", stage="review_security")
    assert 'aria-current="step" data-stage="review"' in client.get(f"/tasks/{task_id}").text

    # ...and so does the fix pass that review asked for.
    _progress_event(app, task_id, message="Fixing", stage="fix")
    assert 'aria-current="step" data-stage="review"' in client.get(f"/tasks/{task_id}").text

    _progress_event(app, task_id, message="Tidying", stage="simplify")
    assert 'aria-current="step" data-stage="simplify"' in client.get(f"/tasks/{task_id}").text


def test_an_escalation_marks_the_work_that_escalated(tmp_path, monkeypatch):
    """The escalation gates are not steps of their own: a reader wants to know
    which work stopped, and the form below says what is being asked."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    _open_gate(app, task_id, "escalate_review")
    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert 'aria-current="step" data-stage="review"' in html


def test_an_open_gate_still_wins_over_the_last_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    _progress_event(app, task_id, message="Read project context", stage="design")
    _open_gate(app, task_id, "review_design")
    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert 'aria-current="step"' in html
    assert ">review design</a>" in html


def test_each_turn_step_is_addressable_by_its_stage(tmp_path, monkeypatch):
    """The page moves the marker as progress arrives, so every step a turn can
    report under has to be findable in the markup."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.workflow.stages import pipeline_steps

    app, task_id = _task_with_docs(tmp_path)
    html = TestClient(app).get(f"/tasks/{task_id}").text
    turns = [s for s in pipeline_steps() if s.turn_label]
    for step in turns:
        assert f'data-stage="{step.turn_label}"' in html
    # Gates carry no marker: nothing reports progress under them.
    strip = html[html.index('<ol class="pipeline">') : html.index("</ol>")]
    assert strip.count("data-stage=") == len(turns)


def test_common_markdown_features_render(tmp_path, monkeypatch):
    """The documents are GFM in practice -- the prompts ask for coverage
    matrices and task checklists -- and without plugins a table came out as a
    paragraph of pipes."""
    from dev_flow_agent.ui.render import markdown_html

    table = markdown_html("| Source | Task |\n| --- | --- |\n| spec | Task 2 |\n")
    assert "<table>" in table and "<th>Source</th>" in table and "<td>spec</td>" in table

    checklist = markdown_html("- [ ] not done\n- [x] done\n")
    assert 'type="checkbox"' in checklist

    assert "<del>" in markdown_html("~~gone~~")


def test_markdown_still_escapes_html(tmp_path, monkeypatch):
    """Plugins must not open a hole: artifacts are model output."""
    from dev_flow_agent.ui.render import markdown_html

    for hostile in (
        "<script>alert(1)</script>",
        "| a |\n| --- |\n| <img src=x onerror=alert(1)> |",
        "<div onclick='x'>hi</div>",
    ):
        out = markdown_html(hostile)
        assert "<script>" not in out
        assert "onerror=" not in out or "&lt;img" in out
        assert "<div" not in out


def test_mermaid_fences_become_diagram_blocks():
    """Design docs carry mermaid; a fenced diagram was rendering as a code dump."""
    from dev_flow_agent.ui.render import markdown_html

    out = markdown_html("```mermaid\ngraph TD;\n  A-->B;\n```\n")
    assert '<pre class="mermaid">' in out
    assert "graph TD;" in out
    # Other languages are untouched.
    assert '<code class="language-python">' in markdown_html("```python\nprint(1)\n```")


def test_a_diagram_cannot_smuggle_markup():
    from dev_flow_agent.ui.render import markdown_html

    out = markdown_html("```mermaid\ngraph TD;\n  A[<script>alert(1)</script>]-->B;\n```")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_mermaid_is_loaded_only_for_a_page_that_has_one(tmp_path, monkeypatch):
    """It is a large dependency; a page with no diagram should not fetch it."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

    app = create_app()
    client = TestClient(app)
    created = client.post(
        "/api/v1/tasks",
        json={"title": "x", "request": "y", "repo_url": "/r", "branch": "main"},
    ).json()
    task_id = created["task_id"]
    app.state.artifacts.write_text(
        f"{task_id}/design.md", "# Design\nplain prose\n", max_bytes=MAX_ARTIFACT_BYTES
    )
    _open_gate(app, task_id, "review_design")
    assert "mermaid.esm.min.mjs" not in client.get(f"/tasks/{task_id}").text

    app.state.artifacts.write_text(
        f"{task_id}/design.md",
        "# Design\n\n```mermaid\ngraph TD;\n  A-->B;\n```\n",
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    assert "mermaid.esm.min.mjs" in client.get(f"/tasks/{task_id}").text


def test_the_task_tree_shows_where_a_long_build_is(tmp_path, monkeypatch):
    """A build that ran as one turn was a black box for an hour."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks.replace_plan_tasks(
        task_id,
        [{"title": "Add the queue"}, {"title": "Drain it"}, {"title": "Wire the route"}],
    )
    app.state.tasks.set_plan_task(task_id, 1, status="done", commit_sha="abc12345def")
    app.state.tasks.set_plan_task(task_id, 2, status="running")
    client = TestClient(app)

    html = client.get(f"/tasks/{task_id}?stage=build").text
    assert "Plan tasks" in html
    assert "1/3" in html
    assert "Add the queue" in html and "Wire the route" in html
    assert "abc12345" in html
    running = 'class="task task-running" data-seq="2" aria-current="step"'
    assert running in html


def test_no_task_tree_before_a_plan_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    assert "Plan tasks" not in TestClient(app).get(f"/tasks/{task_id}?stage=build").text


def test_every_page_route_is_reachable(tmp_path, monkeypatch):
    """A refactor deleted the home routes and only one assertion noticed.
    Reachability is asserted directly, and by asking the app rather than its
    route table, which is a FastAPI internal."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    probe = TestClient(app)

    for path in ("/", "/gates", f"/tasks/{task_id}"):
        assert probe.get(path).status_code == 200, f"GET {path}"

    for path in (
        "/",
        f"/tasks/{task_id}/retry",
        f"/tasks/{task_id}/fail",
        "/gates/nonexistent/answer",
    ):
        # The payloads are wrong on purpose: any status but 404 proves the route
        # exists, and a 404 is what a deleted route looks like.
        assert probe.post(path, data={}, follow_redirects=False).status_code != 404, path
    assert probe.post("/api/v1/tasks", json={}).status_code != 404

    assert probe.get(f"/api/v1/tasks/{task_id}/artifacts/triage.md").status_code == 200


def test_a_gate_event_is_not_read_as_a_running_stage(tmp_path, monkeypatch):
    """`gate_opened` carries its node name in the stage column, and
    `review_plan` starts with the review stage's label. Scanning every event
    type marked a building run as reviewing."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'running' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()
    app.state.runtime.append_event(
        task_ref=task_id, event_type="gate_opened", stage="review_plan", message="waiting"
    )
    _progress_event(app, task_id, message="Read project context", stage="build")

    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert 'aria-current="step" data-stage="review"' not in html


def test_the_stream_resumes_from_a_cursor_not_the_page_watermark(tmp_path, monkeypatch):
    """The server ignores `Last-Event-ID` when an explicit `after_id` is given,
    so a browser's own reconnect resumes from where the page opened and
    re-delivers everything since. The page tracks its own cursor instead."""
    js = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src/dev_flow_agent/ui/static/task.js"
    ).read_text(encoding="utf-8")
    assert "lastEventId" in js, "the page must track what it has seen"
    assert "stream_timeout" in js, "an hour-long run outlives one connection"
    assert "onerror" in js and "reconnect(" in js


def test_the_events_route_honours_an_explicit_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    first = _progress_event(app, task_id, message="one", stage="build")
    _progress_event(app, task_id, message="two", stage="build")
    app.state.runtime.append_event(
        task_ref=task_id, event_type="task_completed", message="done"
    )

    with TestClient(app).stream(
        "GET", f"/api/v1/tasks/{task_id}/events?after_id={first}"
    ) as res:
        body = "".join(res.iter_text())
    assert "two" in body
    assert '"message": "one"' not in body


def test_a_failed_step_is_marked_red_and_says_so(tmp_path, monkeypatch):
    """Colour alone does not survive a screenshot or a reader who does not know
    the convention, so the row says `failed` as well."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.runtime.append_event(
        task_ref=task_id,
        event_type="agent_progress",
        severity="error",
        stage="build",
        message="Ran repository checks",
        payload={"tool": "bash", "status": "issue", "detail": None},
    )
    html = TestClient(app).get(f"/tasks/{task_id}?stage=build").text
    assert 'class="progress-error"' in html
    assert "bash · Ran repository checks · failed" in html


def test_the_tree_rows_are_addressable_and_countable(tmp_path, monkeypatch):
    """The page updates a row in place when a task changes, so each row needs
    an id the stream can find and a sha slot to fill in."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks.replace_plan_tasks(task_id, [{"title": "A"}, {"title": "B"}])
    app.state.tasks.set_plan_task(task_id, 1, status="done", commit_sha="abc12345def")

    html = TestClient(app).get(f"/tasks/{task_id}?stage=build").text
    assert 'data-seq="1"' in html and 'data-seq="2"' in html
    assert 'id="tasks-count"' in html
    # The sha slot exists even when empty, so a finished task can fill it.
    assert html.count('class="task-sha"') == 2

    js = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src/dev_flow_agent/ui/static/task.js"
    ).read_text(encoding="utf-8")
    assert 'addEventListener("plan_task"' in js


def _axis_event(app, task_id, *, name, title, status, verdict=""):
    return app.state.runtime.append_event(
        task_ref=task_id,
        event_type="review_axis",
        severity="error" if status == "failed" else "info",
        stage=f"review_{name}",
        message=f"{title}: {status}",
        payload={"name": name, "title": title, "status": status, "verdict": verdict},
    )


def test_the_review_agents_have_their_own_tree(tmp_path, monkeypatch):
    """Six agents run at once and write into one log, where their lines
    interleave. The tree answers per agent, as it does for the build."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    for name, title in (("correctness", "Correctness"), ("security", "Security")):
        _axis_event(app, task_id, name=name, title=title, status="pending")
    _axis_event(app, task_id, name="security", title="Security", status="running")
    _axis_event(
        app, task_id, name="correctness", title="Correctness", status="done", verdict="clean"
    )

    html = TestClient(app).get(f"/tasks/{task_id}?stage=review").text
    assert "Review agents" in html
    assert 'id="axes-count">1/2' in html
    assert 'data-axis="correctness"' in html and 'data-axis="security"' in html
    assert "clean" in html
    assert 'class="task task-running" data-axis="security" aria-current="step"' in html


def test_an_axis_keeps_its_place_when_it_finishes(tmp_path, monkeypatch):
    """Order is the order they were announced, not the order they finish --
    a list that reshuffles under a reader is unreadable."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    for name in ("correctness", "readability", "security"):
        _axis_event(app, task_id, name=name, title=name.title(), status="pending")
    _axis_event(app, task_id, name="security", title="Security", status="done", verdict="clean")

    html = TestClient(app).get(f"/tasks/{task_id}?stage=review").text
    order = [
        html.index('data-axis="correctness"'),
        html.index('data-axis="readability"'),
        html.index('data-axis="security"'),
    ]
    assert order == sorted(order)


def test_no_review_tree_before_the_review_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    assert "Review agents" not in TestClient(app).get(f"/tasks/{task_id}?stage=review").text


def test_the_strip_marks_where_the_run_is_and_what_is_open(tmp_path, monkeypatch):
    """Two different questions, both answered: the step the run is on is marked
    current, the stage being read is marked selected."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md", "spec.md")
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'running' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()
    _progress_event(app, task_id, message="building", stage="build")

    html = TestClient(app).get(f"/tasks/{task_id}?stage=triage").text
    assert 'aria-current="step" data-stage="build"' in html, "the run is building"
    assert 'aria-current="page">triage</a>' in html, "the reader is on triage"


def test_every_stage_is_reachable_from_the_strip(tmp_path, monkeypatch):
    """Dynamic and clickable: the links come from the flow, so a stage added to
    the YAML is reachable without touching the page."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.workflow.stages import pipeline_steps

    app, task_id = _task_with_docs(tmp_path, "triage.md")
    client = TestClient(app)
    html = client.get(f"/tasks/{task_id}").text

    for step in pipeline_steps():
        if not step.turn_label:
            continue
        link = f"/tasks/{task_id}?stage={step.turn_label}"
        assert link in html, step.label
        assert client.get(link).status_code == 200


def test_an_unknown_stage_falls_back_rather_than_erroring(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    res = TestClient(app).get(f"/tasks/{task_id}?stage=not-a-stage")
    assert res.status_code == 200
    assert "body of triage.md" in res.text


def test_a_stage_does_not_claim_another_stages_branches(tmp_path, monkeypatch):
    """`design_review` starts with `design_`, so inferring ownership from the
    underscore filed the critique's lines under the design. Only a step that
    declares it fans out owns its prefixed labels."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    _progress_event(app, task_id, message="critiquing", detail="by the critique", stage="design_review")
    _progress_event(app, task_id, message="designing", detail="by the design", stage="design")
    client = TestClient(app)

    design = client.get(f"/tasks/{task_id}?stage=design").text
    assert "by the design" in design
    assert "by the critique" not in design

    critique = client.get(f"/tasks/{task_id}?stage=design_review").text
    assert "by the critique" in critique
    assert "by the design" not in critique


def test_the_page_tells_the_stream_whether_this_stage_fans_out(tmp_path, monkeypatch):
    """The same rule has to hold for lines arriving live, and the page cannot
    infer it -- so the server states it."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'running' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()
    client = TestClient(app)

    assert 'data-fanout="1"' in client.get(f"/tasks/{task_id}?stage=review").text
    assert 'data-fanout="0"' in client.get(f"/tasks/{task_id}?stage=design").text


def test_the_stylesheet_is_built_from_tokens_not_scattered_hexes(tmp_path):
    """One neutral ramp with semantic names on top: a dark theme is then a
    redefinition of a dozen variables rather than a rewrite."""
    import re

    css = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src/dev_flow_agent/ui/static/app.css"
    ).read_text(encoding="utf-8")

    tokens, rules = css.split("* { box-sizing: border-box; }", 1)
    for name in ("--surface", "--border", "--text-dim", "--accent", "--danger", "--mono"):
        assert name in tokens, name
    assert "@media (prefers-color-scheme: dark)" in css

    # Below the token block nothing is styled by a literal colour.
    literals = [c for c in re.findall(r"#[0-9a-fA-F]{3,8}\b", rules) if c != "#fff"]
    assert literals == [], f"colour literals outside the token block: {literals}"


def test_no_webfont_is_requested(tmp_path):
    """fonts.googleapis.com does not resolve from the corp network, so a
    webfont is a blank flash and then the fallback anyway."""
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "src/dev_flow_agent/ui"
    for path in list(root.rglob("*.css")) + list(root.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        # Comments may name the domain -- that is where the reason is recorded.
        # What must not appear is a request for it.
        code = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        code = re.sub(r"\{#.*?#\}", "", code, flags=re.DOTALL)
        code = re.sub(r"<!--.*?-->", "", code, flags=re.DOTALL)
        for domain in ("fonts.googleapis.com", "fonts.gstatic.com"):
            assert domain not in code, f"{path.name} requests {domain}"


def test_the_page_moves_on_when_a_stage_finishes(tmp_path, monkeypatch):
    """Approving triage starts the spec, but the spec has not reported yet --
    the newest progress still says triage. The page followed that and stayed
    put."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'running' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()
    client = TestClient(app)

    # Triage runs and reports.
    _progress_event(app, task_id, message="classifying", stage="triage")
    assert 'aria-current="step" data-stage="triage"' in client.get(f"/tasks/{task_id}").text

    # Triage finishes. The spec is running but has said nothing yet.
    app.state.artifacts.write_text(
        f"{task_id}/triage.md", "# Triage\nKIND: feature-dev\n", max_bytes=1_048_576
    )
    app.state.runtime.append_event(
        task_ref=task_id,
        event_type="document_written",
        stage="triage",
        message="wrote triage.md",
        payload={"artifact": "triage.md"},
    )

    html = client.get(f"/tasks/{task_id}").text
    assert 'aria-current="step" data-stage="spec"' in html, "the run has moved on"
    assert 'aria-current="step" data-stage="triage"' not in html


def test_a_reporting_stage_still_wins_when_it_is_further(tmp_path, monkeypatch):
    """The furthest signal, not the newest. Review reports under a fanned-out
    label while the last document written is the plan -- the documents alone
    would place the run at build, one step behind where it is."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.workflow.stages import stages

    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'running' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()
    for stage in stages():
        # Everything the plan produces, but nothing from the stages that come
        # after building.
        if stage.turn_label in ("review", "enforce"):
            continue
        app.state.runtime.append_event(
            task_ref=task_id,
            event_type="document_written",
            stage=stage.label,
            message=f"wrote {stage.artifact}",
            payload={"artifact": stage.artifact},
        )
    client = TestClient(app)
    # Documents alone put the run at the step after the plan: build.
    assert 'aria-current="step" data-stage="build"' in client.get(f"/tasks/{task_id}").text

    _progress_event(app, task_id, message="auditing", stage="review_security")
    html = client.get(f"/tasks/{task_id}").text
    assert 'aria-current="step" data-stage="review"' in html
    assert 'aria-current="step" data-stage="build"' not in html


def test_parallel_agents_get_a_lane_each(tmp_path, monkeypatch):
    """Six reviewers writing into one list is six voices in one transcript:
    every line has to be read to find out whose it is."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    for name, title in (("correctness", "Correctness"), ("security", "Security")):
        _axis_event(app, task_id, name=name, title=title, status="running")
    _progress_event(app, task_id, message="reading", detail="by correctness", stage="review_correctness")
    _progress_event(app, task_id, message="auditing", detail="by security", stage="review_security")

    html = TestClient(app).get(f"/tasks/{task_id}?stage=review").text
    assert 'class="lanes split"' in html
    assert 'data-lane-list="review_correctness"' in html
    assert 'data-lane-list="review_security"' in html

    # Each line sits in its own agent's lane, not in a shared list.
    correctness = html.split('data-lane-list="review_correctness"')[1].split("</div>")[1]
    assert "by security" not in html.split('data-lane-list="review_correctness"')[1].split("</section>")[0]
    assert "by correctness" not in html.split('data-lane-list="review_security"')[1].split("</section>")[0]
    # The lane is titled by its agent, with that agent's own state.
    assert "Correctness" in html and "Security" in html


def test_a_single_agent_stage_is_not_split(tmp_path, monkeypatch):
    """Lanes are for concurrency. One agent gets one list and no header."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    _progress_event(app, task_id, message="building", detail="one voice", stage="build")

    html = TestClient(app).get(f"/tasks/{task_id}?stage=build").text
    assert 'class="lanes"' in html and "lanes split" not in html
    assert html.count("lane-title") == 0
    assert "one voice" in html


def test_an_axis_that_starts_later_still_gets_a_lane(tmp_path, monkeypatch):
    """The page renders lanes for the announced agents; the stream creates one
    for an agent that starts after the page was rendered."""
    js = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src/dev_flow_agent/ui/static/task.js"
    ).read_text(encoding="utf-8")
    assert "function laneFor" in js
    assert "data-lane-list=" in js


def test_a_repair_round_appears_as_its_own_step(tmp_path, monkeypatch):
    """A run that loops back is not going backwards -- it is doing something it
    had not done before. Moving the marker back to `build` says the opposite."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks.replace_plan_tasks(task_id, [{"title": "Build it"}])
    app.state.tasks.set_plan_task(task_id, 1, status="done")
    app.state.tasks.append_plan_task(
        task_id, title="Fix the test-enforcement gate", origin="enforcement"
    )
    app.state.tasks.set_plan_task(task_id, 2, status="running")

    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert "fix test gate" in html
    assert 'class="repair repair-running"' in html
    # The marker is on the repair, not on the build step executing it.
    assert 'aria-current="step" data-stage="build"' not in html


def test_repair_rounds_are_numbered(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path)
    app.state.tasks.replace_plan_tasks(task_id, [{"title": "Build it"}])
    for _ in range(2):
        app.state.tasks.append_plan_task(
            task_id, title="Address the review's Critical findings", origin="review"
        )
    html = TestClient(app).get(f"/tasks/{task_id}").text
    assert "fix review findings" in html
    assert "fix review findings 2" in html


def test_a_run_sent_back_to_build_is_shown_at_build(tmp_path, monkeypatch):
    """The gate failed, so the run is building again -- but `enforcement.md`
    from the failed pass is still written. Placing the run at the furthest
    point any signal reached opened the review, where nothing was happening."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "plan.md", "enforcement.md")
    app.state.runtime.append_event(
        task_ref=task_id,
        event_type="agent_progress",
        message="fixing the failing test",
        stage="build",
    )

    html = TestClient(app).get(f"/tasks/{task_id}").text
    strip = html[html.index('class="pipeline"') : html.index("</ol>")]
    current = [line for line in strip.splitlines() if 'aria-current="step"' in line]
    assert len(current) == 1
    assert 'data-stage="build"' in current[0]


def test_a_run_waiting_on_no_gate_still_says_where_it_is(tmp_path, monkeypatch):
    """A cancelled gate keeps its deterministic id, so a run could suspend on a
    gate no inbox lists. The strip then marked nothing at all, which reads as a
    broken page rather than a stuck run."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "plan.md")
    app.state.runtime.append_event(
        task_ref=task_id,
        event_type="agent_progress",
        message="running the gate",
        stage="enforce",
    )
    app.state.tasks._conn.execute(
        "UPDATE df_tasks SET status = 'waiting_human' WHERE task_id = ?", (task_id,)
    )
    app.state.tasks._conn.commit()

    html = TestClient(app).get(f"/tasks/{task_id}").text
    strip = html[html.index('class="pipeline"') : html.index("</ol>")]
    current = [line for line in strip.splitlines() if 'aria-current="step"' in line]
    assert len(current) == 1
    assert 'data-stage="enforce"' in current[0]


def test_every_gate_the_flow_can_open_belongs_to_a_step():
    """A gate node that folds into no step leaves the page with nothing to mark
    as current and no form to answer it -- which is what a run stopped at
    `escalate_enforcement` looked like: a blank pipeline on a live task."""
    from agent_core.workflow import WorkflowSpec

    from dev_flow_agent.workflow import FLOW_PATH
    from dev_flow_agent.workflow.stages import pipeline_steps, stages, step_for_node

    spec = WorkflowSpec.from_file(FLOW_PATH)
    gates = [node.name for node in spec.nodes if node.uses == "human_gate"]
    assert gates, "the flow has gates"

    owns = {stage.gate for stage in stages() if stage.gate}
    for gate in gates:
        assert gate in owns or step_for_node(gate) is not None, gate
    assert pipeline_steps()


def test_the_page_follows_the_run_unless_a_stage_was_asked_for(tmp_path, monkeypatch):
    """Approving a gate looked like nothing happened: the strip moved to the
    next stage while the panel kept showing the finished one, with a log that
    had stopped growing. The page follows the run -- unless the reader asked
    for a particular stage, who is reading it on purpose."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    app, task_id = _task_with_docs(tmp_path, "triage.md")
    client = TestClient(app)

    watching = client.get(f"/tasks/{task_id}").text
    assert 'data-pinned="0"' in watching

    reading = client.get(f"/tasks/{task_id}?stage=triage").text
    assert 'data-pinned="1"' in reading


def test_the_client_only_follows_forward():
    """A late line from a stage already finished must not pull the page back
    to where the run no longer is."""
    from dev_flow_agent import app as app_module

    script = (app_module.STATIC_DIR / "task.js").read_text(encoding="utf-8")
    assert "function isLater(" in script
    assert "if (!mine && !pinned && isLater(stage)) reload();" in script
    # The guard is the pipeline's own order, read off the strip.
    assert 'steps.querySelectorAll("[data-stage]")' in script
