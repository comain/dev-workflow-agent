(function () {
  const script = document.currentScript;
  const taskId = script && script.dataset.taskId;
  const root = (script && script.dataset.root) || "";
  // The gate this page was rendered with, if any. A `gate_opened` naming *this*
  // gate is history, not news -- reloading on it would reload forever.
  const shownGate = (script && script.dataset.gateId) || "";
  // A page already showing a running task is not stale, so it does not reload
  // when work restarts -- otherwise every claim would reload it.
  const shownStatus = (script && script.dataset.status) || "";
  // The page shows one stage at a time, so a line from another stage is not
  // this view's business -- it lands where it belongs when that stage is
  // opened, rather than mixing six agents into one list again.
  const shownStage = (script && script.dataset.stage) || "";
  // A reader who asked for a stage stays on it; one who opened the task is
  // watching the run and follows it.
  const pinned = (script && script.dataset.pinned) === "1";
  // Whether this stage's work fans out into branches reporting under
  // `<stage>_<branch>`. Declared by the server: inferring it from the
  // underscore would file `design_review` lines under `design`.
  const shownFansOut = (script && script.dataset.fanout) === "1";
  const lanes = document.getElementById("progress-lanes");
  if (!taskId || !lanes) return;
  const empty = document.getElementById("progress-empty");
  const steps = document.querySelector(".pipeline");
  const tasks = document.querySelector("#tasks .tasks");
  const axes = document.querySelector("#axes .tasks");

  function countIn(root, counterId) {
    const counter = document.getElementById(counterId);
    if (!counter || !root) return;
    const all = root.querySelectorAll(".task").length;
    const done = root.querySelectorAll(".task-done, .task-skipped").length;
    counter.textContent = done + "/" + all;
  }

  function updateRow(root, row, info, trailing) {
    if (!row) return;
    row.className = "task task-" + (info.status || "pending");
    const status = row.querySelector(".task-status");
    if (status) status.textContent = info.status || "";
    const slot = row.querySelector(".task-sha");
    if (slot) slot.textContent = trailing || "";
    if (info.status === "running") {
      row.setAttribute("aria-current", "step");
    } else {
      row.removeAttribute("aria-current");
    }
  }

  // Everything already in the log was rendered into this page, so the stream
  // starts after it, and every event moves the mark. The server ignores
  // `Last-Event-ID` whenever an explicit `after_id` is given, so a browser's
  // own reconnect would resume from where the *page* opened and re-deliver
  // everything since -- this is the cursor that stops that.
  let cursor = parseInt((script && script.dataset.afterId) || "0", 10) || 0;
  let source = null;
  let reloaded = false;
  let retry = null;

  function laneFor(stage) {
    // Each agent writes into its own lane. Six of them in one list is six
    // voices in one transcript: every line has to be read to find out whose
    // it is, and the interleaving carries no meaning.
    const key = shownFansOut ? stage || "" : "";
    const existing = lanes.querySelector('[data-lane-list="' + key + '"]');
    if (existing) return existing;
    if (!shownFansOut) return lanes.querySelector("[data-lane-list]");

    // An agent that starts after the page was rendered still gets a lane.
    const section = document.createElement("section");
    section.className = "lane";
    section.dataset.lane = key;
    const title = document.createElement("h4");
    title.className = "lane-title";
    title.textContent = key;
    const list = document.createElement("div");
    list.className = "progress-list";
    list.dataset.laneList = key;
    section.appendChild(title);
    section.appendChild(list);
    lanes.appendChild(section);
    return list;
  }

  function append(payload) {
    if (empty) empty.hidden = true;
    const list = laneFor(payload && payload.stage);
    if (!list) return;
    const row = document.createElement("div");
    const info = (payload && payload.payload) || {};
    // `detail` is the redacted synopsis -- the file or step actually touched.
    // The message alone is a category ("Agent update"), which on its own tells
    // a reader nothing, so it is the fallback rather than the first choice.
    const msg = info.detail || (payload && payload.message) || "progress";
    const parts = [];
    if (payload && payload.stage) parts.push(payload.stage);
    if (info.tool) parts.push(info.tool);
    parts.push(msg);
    // Say it failed as well as showing it: colour does not survive a
    // screenshot, a copy-paste, or a reader who does not know the convention.
    if (info.status === "issue") parts.push("failed");

    // The clock is what makes a list of updates a timeline: which step was slow
    // and where a run stalled are only answerable against it.
    const when = document.createElement("time");
    const stamp = payload && payload.created_at ? new Date(payload.created_at) : new Date();
    if (!isNaN(stamp.getTime())) {
      when.dateTime = stamp.toISOString();
      when.textContent = stamp.toLocaleTimeString([], { hour12: false });
    }
    when.className = "progress-time";
    row.appendChild(when);
    row.appendChild(document.createTextNode(parts.join(" · ")));
    if (payload && payload.severity === "error") row.className = "progress-error";
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
  }

  // Rows rendered with the page carry a UTC stamp; the clock a reader wants is
  // their own, and the formatter for it lives here rather than on the server.
  Array.prototype.forEach.call(lanes.querySelectorAll("time[datetime]"), function (el) {
    const at = new Date(el.dateTime);
    if (!isNaN(at.getTime())) el.textContent = at.toLocaleTimeString([], { hour12: false });
  });
  if (lanes.querySelector(".progress-list > div") && empty) empty.hidden = true;

  function parse(ev) {
    try {
      return JSON.parse(ev.data);
    } catch (e) {
      return { message: ev.data };
    }
  }

  function advance(ev) {
    const id = parseInt(ev.lastEventId || "0", 10);
    if (id > cursor) cursor = id;
    return id;
  }

  function reload() {
    if (reloaded) return;
    reloaded = true;
    if (source) source.close();
    location.reload();
  }

  function isLater(stage) {
    // Only forward. A late line from a stage already finished would otherwise
    // reload the page back to where it no longer is.
    if (!steps || !stage || !shownStage) return Boolean(stage);
    const order = Array.prototype.map.call(
      steps.querySelectorAll("[data-stage]"),
      function (el) { return el.getAttribute("data-stage"); }
    );
    const here = order.indexOf(shownStage);
    const there = order.indexOf(stage.split("_")[0] === shownStage ? shownStage : stage);
    return here >= 0 && there > here;
  }

  function markStage(stage) {
    if (!steps || !stage) return;
    const target = steps.querySelector('[data-stage="' + stage + '"]');
    if (!target || target.hasAttribute("aria-current")) return;
    Array.prototype.forEach.call(steps.querySelectorAll("[aria-current]"), function (el) {
      el.removeAttribute("aria-current");
    });
    target.setAttribute("aria-current", "step");
  }

  function reconnect(delay) {
    if (reloaded || retry) return;
    if (source) source.close();
    retry = window.setTimeout(function () {
      retry = null;
      connect();
    }, delay);
  }

  function connect() {
    if (reloaded) return;
    source = new EventSource(
      root + "/api/v1/tasks/" + encodeURIComponent(taskId) + "/events?after_id=" + cursor
    );

    source.addEventListener("agent_progress", function (ev) {
      advance(ev);
      const payload = parse(ev);
      const stage = (payload && payload.stage) || "";
      const mine =
        !shownStage ||
        stage === shownStage ||
        (shownFansOut && stage.indexOf(shownStage + "_") === 0);
      if (mine) append(payload);
      // The strip was rendered with the page and would otherwise hold the stage
      // that was running then, while the rows below it report a later one.
      markStage(payload && payload.stage);
      // ...and the panel has to follow it. Marking the strip alone left the
      // page showing the stage that had finished, with a log that had stopped
      // growing: approving a gate looked like nothing happened.
      if (!mine && !pinned && isLater(stage)) reload();
    });

    // Stored rows arrive as *named* events, so `onmessage` never sees them;
    // each type the page reacts to has to be bound by name.
    source.addEventListener("gate_opened", function (ev) {
      advance(ev);
      const gateId = (parse(ev).payload || {}).gate_id || "";
      if (gateId !== shownGate) reload();
    });

    source.addEventListener("run_started", function (ev) {
      advance(ev);
      if (shownStatus !== "running") reload();
    });

    source.addEventListener("document_written", advance);

    // The tree is the progress view for a build that runs for an hour. It was
    // rendered with the page, so a task finishing was invisible until someone
    // reloaded -- which is how a build looks stalled while it is working.
    source.addEventListener("plan_task", function (ev) {
      advance(ev);
      const info = (parse(ev) || {}).payload || {};
      const sha = info.commit_sha ? String(info.commit_sha).slice(0, 8) : "";
      updateRow(tasks, tasks && tasks.querySelector('[data-seq="' + info.seq + '"]'), info, sha);
      countIn(tasks, "tasks-count");
    });

    // Six review agents run at once and write into one log. This is the row
    // each one owns, so their progress is legible apart from each other.
    source.addEventListener("review_axis", function (ev) {
      advance(ev);
      const info = (parse(ev) || {}).payload || {};
      updateRow(
        axes,
        axes && axes.querySelector('[data-axis="' + info.name + '"]'),
        info,
        info.verdict || ""
      );
      countIn(axes, "axes-count");
    });

    // This script is only served for a task that is still running, so a
    // terminal event -- replayed or live -- means the page is out of date.
    ["task_completed", "task_failed", "task_cancelled"].forEach(function (name) {
      source.addEventListener(name, reload);
    });

    // The server bounds a connection at an hour and says so before closing.
    // Without this the tab goes quiet on a run longer than that.
    source.addEventListener("stream_timeout", function () {
      reconnect(500);
    });

    // A dropped connection -- a deploy, a proxy timeout -- reconnects from the
    // cursor rather than leaving the page frozen or replaying what it has.
    source.onerror = function () {
      if (source && source.readyState === EventSource.CLOSED) reconnect(2000);
    };
  }

  connect();
})();
