"""Create-task API: enqueue, idempotency, no operator in the body."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from dev_flow_agent.app import create_app

    return TestClient(create_app())


def _body(**over):
    data = {
        "title": "SSE inbox",
        "request": "Watch agent progress",
        "repo_url": "/tmp/repo",
        "branch": "main",
    }
    data.update(over)
    return data


def test_create_returns_201_and_queued_task(client):
    res = client.post("/api/v1/tasks", json=_body())
    assert res.status_code == 201
    payload = res.json()
    assert payload["status"] == "queued"
    assert payload["title"] == "SSE inbox"
    assert "operator" not in payload or payload.get("created_by") != "sneaky"
    assert payload["task_id"]


def test_operator_in_the_body_is_ignored(client):
    res = client.post("/api/v1/tasks", json=_body(operator="sneaky", created_by="sneaky"))
    assert res.status_code == 201
    assert res.json().get("created_by") != "sneaky"


def test_idempotent_replay_same_key_same_body(client):
    headers = {"Idempotency-Key": "k1"}
    first = client.post("/api/v1/tasks", json=_body(), headers=headers)
    second = client.post("/api/v1/tasks", json=_body(), headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]


def test_idempotent_conflict_same_key_different_body(client):
    headers = {"Idempotency-Key": "k2"}
    client.post("/api/v1/tasks", json=_body(), headers=headers)
    res = client.post("/api/v1/tasks", json=_body(title="other"), headers=headers)
    assert res.status_code == 422


def test_unknown_artifact_is_404(client):
    created = client.post("/api/v1/tasks", json=_body()).json()
    res = client.get(f"/api/v1/tasks/{created['task_id']}/artifacts/secret.env")
    assert res.status_code == 404


def test_missing_artifact_is_404(client):
    created = client.post("/api/v1/tasks", json=_body()).json()
    res = client.get(f"/api/v1/tasks/{created['task_id']}/artifacts/triage.md")
    assert res.status_code == 404


def test_a_malformed_body_is_the_callers_fault(tmp_path, monkeypatch):
    """`await request.json()` raised out of the handler, so a bad body answered
    500 -- a server fault for something the caller got wrong."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    from fastapi.testclient import TestClient

    from dev_flow_agent.app import create_app

    client = TestClient(create_app())
    assert client.post("/api/v1/tasks", content=b"not json").status_code == 400
    assert client.post("/api/v1/tasks", json=["a", "list"]).status_code == 422


def test_two_stores_can_migrate_at_once(tmp_path):
    """`dev` runs the server and the worker in one process, each with its own
    connection, so two migrations start together. The loser's failure is not a
    failure -- but it crashed the service on the first start after a deploy."""
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    from dev_flow_agent.db import TaskStore

    path = tmp_path / "tasks.db"
    # A database as an earlier release left it: no `origin` column.
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE df_plan_tasks (task_id TEXT NOT NULL, seq INTEGER NOT NULL,"
        " title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL"
        " DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, commit_sha TEXT,"
        " updated_at TEXT NOT NULL, PRIMARY KEY (task_id, seq))"
    )
    conn.commit()
    conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: TaskStore(path), range(4)))

    columns = {r["name"] for r in stores[0]._conn.execute("PRAGMA table_info(df_plan_tasks)")}
    assert "origin" in columns


def _lookup(payload, status=200):
    import json

    def transport(method, url, body, headers):
        assert headers["Authorization"].startswith("Bearer ")
        # The gateway routes on the name while the socket goes to its address.
        assert headers["Host"] == "catalog.example.com"
        return status, json.dumps(payload).encode()

    return transport


def test_an_application_name_resolves_to_its_repository():
    """People name applications, not clone URLs: `sample_app` is
    what a ticket says, and the catalog knows where its code lives."""
    from dev_flow_agent.chongxiao import Chongxiao

    found = Chongxiao(
        access_token="k",
        transport=_lookup(
            {
                "status": 0,
                "data": {
                    "code": "sample_app",
                    "git_path": "git@git.example.com:group/sample-app.git",
                    "description": "sample application",
                },
            }
        ),
    )
    assert found.service("sample_app").description == "sample application"
    # ...in the HTTPS form the pipeline's token can clone.
    assert found.repo_url("sample_app") == (
        "https://git.example.com/group/sample-app.git"
    )


def test_a_name_that_resolves_to_nothing_says_so():
    from dev_flow_agent.chongxiao import Chongxiao, LookupFailed

    no_repo = Chongxiao(access_token="k", transport=_lookup({"status": 0, "data": {"code": "x"}}))
    with pytest.raises(LookupFailed, match="no repository recorded"):
        no_repo.service("x")

    refused = Chongxiao(access_token="k", transport=_lookup({}, status=403))
    with pytest.raises(LookupFailed, match="access key was refused"):
        refused.service("x")

    # Without a key it says which knob is missing rather than failing at a socket.
    with pytest.raises(LookupFailed, match="DFA_SSO_ACCESS_TOKEN"):
        Chongxiao(access_token="").service("x")


def test_a_url_is_left_alone():
    """Only a bare name is a lookup. Anything with a scheme, an SSH shape or a
    path separator is a repository someone typed out in full."""
    from dev_flow_agent.chongxiao import looks_like_repo

    assert looks_like_repo("https://git.example.com/Island/sample-web")
    assert looks_like_repo("git@git.example.com:group/sample-app.git")
    assert looks_like_repo("/tmp/repo")
    assert not looks_like_repo("sample_app")
    assert not looks_like_repo("")


def test_the_form_accepts_an_application_name(tmp_path, monkeypatch):
    """End to end: the name goes in the box, the repository comes out."""
    monkeypatch.setenv("DFA_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("DFA_SSO_ACCESS_TOKEN", "k")
    from dev_flow_agent import chongxiao
    from dev_flow_agent.app import create_app

    real = chongxiao.Chongxiao

    def answering(payload):
        def build(**kw):
            return real(**{**kw, "transport": _lookup(payload)})

        return build

    monkeypatch.setattr(
        chongxiao,
        "Chongxiao",
        answering(
            {
                "status": 0,
                "data": {
                    "code": "sample_service",
                    "git_path": "git@git.example.com:wms/sample-service.git",
                },
            }
        ),
    )
    client = TestClient(create_app())
    created = client.post(
        "/api/v1/tasks",
        json={"title": "t", "request": "r", "repo_url": "sample_service", "branch": "main"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["repo_url"] == (
        "https://git.example.com/wms/sample-service.git"
    )

    # A name nobody knows is refused while the person is still looking at the
    # form, rather than four minutes later in a run that failed at the clone.
    monkeypatch.setattr(chongxiao, "Chongxiao", answering({"status": 1, "msg": "not found"}))
    refused = client.post(
        "/api/v1/tasks",
        json={"title": "t2", "request": "r", "repo_url": "no_such_app", "branch": "main"},
    )
    assert refused.status_code == 422
    assert "not found" in refused.text
