"""Offline ASGI and coordinator contracts. No network, model, or scientific process."""

import hashlib
import threading

import pytest
from fastapi.testclient import TestClient
from filelock import Timeout
from test_llm import socket_free_fake_client_loop  # noqa: F401
from test_text_entry import text_environment

from orca_agent import entrypoints
from orca_agent.models import Goal, Request
from orca_agent.store import BudgetExceeded, StoreError
from orca_agent.web import Coordinator, create_app


@pytest.fixture
def env(tmp_path):
    return text_environment(tmp_path)


@pytest.fixture
def client(env):
    store, config = env
    app = create_app(config, store=store)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.get("/api/bootstrap").json()
        client.headers.update({"X-Local-Token": bootstrap["token"],
                               "Origin": "http://127.0.0.1:8765"})
        yield client


def new_run(env, key="submit_one"):
    store, config = env
    return entrypoints.create_text(store, config, "计算水分子初始结构的电子能", submission_id=key)[0]


def test_same_submit_across_restarts_returns_same_run_and_budget(env):
    store, config = env
    run = new_run(env)
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    again, created = entrypoints.create_text(store, config, "计算水分子初始结构的电子能",
                                              submission_id="submit_one")
    assert not created and again.id == run.id
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    with pytest.raises(StoreError, match="different content"):
        entrypoints.create_text(store, config, "不同请求", submission_id="submit_one")


def test_intake_crash_never_creates_duplicate(env, monkeypatch):
    store, config = env
    original = entrypoints.natural.initialize_text

    def crash(*args):
        original(*args)
        raise RuntimeError("crash after persistence")

    monkeypatch.setattr(entrypoints.natural, "initialize_text", crash)
    with pytest.raises(RuntimeError):
        new_run(env)
    with pytest.raises(StoreError, match="interrupted intake"):
        new_run(env)
    assert len(list(store.path("runs").glob("*/run.json"))) == 1


def test_message_id_deduplicates_even_after_consumption_and_keeps_history(env):
    store, _ = env
    run = new_run(env)
    for index in range(30):
        message_id = store.enqueue_message(run.id, f"补充 {index}", message_id=f"message_{index}")
        run = store.load_run(run.id)
        run.processed_messages = [m["id"] for m in store.read_control(run.id)["messages"]]
        store.save_run(run)
        generation = store.read_control(run.id)["generation"]
        assert store.enqueue_message(run.id, f"补充 {index}", message_id=message_id) == message_id
        assert store.read_control(run.id)["generation"] == generation
    assert entrypoints.messages(store, run.id)["total"] == 31
    assert entrypoints.messages(store, run.id, offset=20, limit=10)["next_offset"] == 30
    assert store.load_run(run.id).usage == run.usage
    with pytest.raises(StoreError, match="different content"):
        store.enqueue_message(run.id, "篡改", message_id="message_1")


def test_pending_messages_still_bounded(env):
    store, _ = env
    run = new_run(env)
    for i in range(23):
        store.enqueue_message(run.id, f"message {i}")
    with pytest.raises(BudgetExceeded, match="pending"):
        store.enqueue_message(run.id, "one too many")


def test_read_endpoints_leave_run_bytes_and_cost_unchanged(env, client, monkeypatch):
    store, _ = env
    run = new_run(env)
    before = {p.relative_to(store.root): p.read_bytes() for p in store.path(f"runs/{run.id}").rglob("*.json")}
    monkeypatch.setattr(entrypoints, "execute", lambda *a, **k: pytest.fail("read started work"))
    for route in ["", "/report", "/messages", "/artifacts"]:
        response = client.get(f"/api/runs/{run.id}{route}")
        assert response.status_code == 200, response.text
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/runs").json()["total"] == 1
    assert all(store.path(path).read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example:8765"}, {"Origin": "https://attacker.example"},
    {"Sec-Fetch-Site": "cross-site"}, {"X-Local-Token": "wrong"},
])
def test_http_boundary_rejects_cross_site_and_wrong_token(client, headers):
    assert client.get("/api/runs", headers=headers).status_code == 403


def test_post_requires_origin_and_bounded_json(client):
    assert client.post("/api/runs", headers={"Origin": "null"}, json={}).status_code == 403
    assert client.post("/api/runs", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/runs", content=b" " * 65537,
                       headers={"Content-Type": "application/json"}).status_code == 413
    response = client.post("/api/runs", json={"text": "sk-testsecret12345678", "shell": "secret"})
    assert response.status_code == 409 and "sk-testsecret" not in response.text


def test_submission_replay_never_dispatches_twice(client, monkeypatch):
    starts = []
    monkeypatch.setattr(client.app.state.coordinator, "start",
                        lambda run_id, **kw: starts.append(run_id) or True)
    body = {"text": "计算水分子单点电子能", "submission_id": "web_first"}
    first = client.post("/api/runs", json=body)
    again = client.post("/api/runs", json=body)
    assert first.status_code == again.status_code == 202
    assert first.json()["run_id"] == again.json()["run_id"]
    assert len(starts) == 1 and again.json()["started"] is False


def test_busy_submission_returns_saved_id_without_retry_loop(client, monkeypatch):
    def busy(*a, **k):
        raise StoreError("busy")
    monkeypatch.setattr(client.app.state.coordinator, "start", busy)
    response = client.post("/api/runs", json={"text": "水单点电子能", "submission_id": "web_busy"})
    assert response.status_code == 202
    assert response.json()["run_id"] and response.json()["started"] is False


def test_message_and_control_are_signals_not_execution(env, client, monkeypatch):
    store, _ = env
    run = new_run(env)
    monkeypatch.setattr(entrypoints, "execute", lambda *a, **k: pytest.fail("implicit resume"))
    body = {"text": "沿用原来条件", "message_id": "web_message"}
    for _ in range(2):
        assert client.post(f"/api/runs/{run.id}/messages", json=body).status_code == 202
    assert len(store.read_control(run.id)["messages"]) == 2
    assert client.post(f"/api/runs/{run.id}/control", json={"action": "pause"}).status_code == 202
    assert store.read_signal(run.id) == "pause"
    assert store.load_run(run.id).state == "ready"


def test_download_and_queries_bound_to_run_hash_and_typed_path(env, client, tmp_path):
    store, _ = env
    source = tmp_path / "values.json"
    source.write_text('{"values":[1,2,3],"unit":"unknown"}')
    artifact = store.import_artifact(source, "external_evidence")
    run = store.create_run(Request(goals=[Goal(id="q", port="unresolved",
                                               minimum_check_version="unresolved-1")]), None)
    url = f"/api/runs/{run.id}"
    assert client.get(f"{url}/artifacts/{artifact.id}/download").status_code == 409
    run.permission.artifact_ids.append(artifact.id)
    # Production permissions are immutable; create a separate authorized Run.
    authorized = store.create_run(store.load_request(run), None, run.permission)
    url = f"/api/runs/{authorized.id}"
    response = client.get(f"{url}/artifacts/{artifact.id}/download")
    assert response.content == source.read_bytes()
    assert response.headers["x-artifact-sha256"] == hashlib.sha256(response.content).hexdigest()
    value = client.post(f"{url}/evidence", json={"tool": "evidence.value", "parameters": {
        "artifact_id": artifact.id, "path": [{"kind": "key", "key": "values"},
                                            {"kind": "index", "index": 2}]}})
    assert value.status_code == 200 and value.json()["value"] == 3
    assert client.post(f"{url}/evidence", json={"tool": "orca.sp", "parameters": {}}).status_code == 409
    store.artifact_path(artifact.id).write_text("changed")
    assert client.get(f"{url}/artifacts/{artifact.id}/download").status_code == 409


def test_coordinator_refresh_restart_shutdown_and_duplicate_start(env, monkeypatch):
    store, config = env
    run = new_run(env)
    started, release = threading.Event(), threading.Event()
    calls = []

    def execute(*args, **kw):
        calls.append(kw)
        started.set()
        assert release.wait(3)

    monkeypatch.setattr(entrypoints, "execute", execute)
    coordinator = Coordinator(store, config)
    assert not coordinator.active(run.id) and not calls
    assert coordinator.start(run.id, resume=True)
    assert started.wait(3)
    assert not coordinator.start(run.id, resume=True)
    assert coordinator.close(timeout=0) == [run.id]
    assert store.read_signal(run.id) == "pause" and calls[0]["stop_event"].is_set()
    release.set()
    coordinator.close(timeout=3)
    restarted = Coordinator(store, config)
    assert not restarted.active(run.id) and len(calls) == 1
    assert store.load_run(run.id).usage.model_calls == 0


def test_second_coordinator_cannot_compete_with_cli_lock(env):
    store, config = env
    run = new_run(env)
    failures = []
    with store.run_lock(run.id):
        def competing():
            try:
                Coordinator(store, config).start(run.id, resume=True)
            except Timeout:
                failures.append("locked")
        thread = threading.Thread(target=competing)
        thread.start()
        thread.join(3)
    assert failures == ["locked"]


def test_shutdown_event_prevents_resume_from_clearing_pause(env, monkeypatch):
    store, config = env
    run = new_run(env)
    stop = threading.Event()
    stop.set()
    store.signal(run.id, "pause")
    result = entrypoints.execute(store, config, run.id, resume=True, stop_event=stop)
    assert result.state == "paused" and store.read_signal(run.id) == "pause"
    assert result.usage.model_calls == result.usage.orca_starts_actual == 0


def test_batch_runs_cannot_escape_original_driver(env):
    store, config = env
    run = new_run(env)
    run.batch_category = "development"
    store.save_run(run)
    with pytest.raises(StoreError, match="original batch"):
        Coordinator(store, config).start(run.id, resume=True)


def test_restart_lifespan_does_not_automatically_dispatch(env, monkeypatch):
    store, config = env
    run = new_run(env)
    store.signal(run.id, "pause")
    monkeypatch.setattr(entrypoints, "execute", lambda *a, **k: pytest.fail("restart resumed"))
    with TestClient(create_app(config, store=store), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 200
    assert store.read_signal(run.id) == "pause"
