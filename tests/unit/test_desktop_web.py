"""Offline desktop launcher checks: no real process, browser or network."""

import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_llm import socket_free_fake_client_loop  # noqa: F401
from test_text_entry import text_environment

from orca_agent import entrypoints
from orca_agent.web import Coordinator, create_app, service_identity
from scripts import desktop_web as launcher


@pytest.fixture
def launch_env(tmp_path, monkeypatch):
    config_path = tmp_path / "local config.toml"
    config_path.write_text("", encoding="utf-8")
    config = SimpleNamespace(data_root=tmp_path / "data")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(launcher, "load_config", lambda _: config)
    monkeypatch.setattr(launcher, "_logs", lambda _: log_dir)
    return config_path, config, log_dir


def _response(raw, *, status=200):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == launcher.HEALTH_BYTES + 1
            return raw[:limit]

    result = Response()
    result.status = status
    return result


def _opener(monkeypatch, response=None, *, error=None):
    calls = []

    def open_request(request, *, timeout):
        calls.append((request, timeout))
        if error is not None:
            raise error
        return response

    handlers = []

    def build(*items):
        handlers.extend(items)
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(launcher.urllib.request, "build_opener", build)
    return calls, handlers


def test_health_is_exact_workspace_identity_and_disables_proxy_and_redirects(tmp_path, monkeypatch):
    expected = service_identity(tmp_path / "data")
    calls, handlers = _opener(monkeypatch, _response(json.dumps(expected).encode()))
    assert launcher._probe(8765, expected, timeout=.4)
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8765/health" and timeout == .4
    proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
    assert proxy.proxies == {}
    redirect = next(item for item in handlers if isinstance(item, launcher._NoRedirect))
    assert redirect.redirect_request(request, None, 302, "redirect", {},
                                     "https://external.invalid/") is None
    assert expected["automatic_run_resume"] is False
    assert expected != service_identity(tmp_path / "other")
    assert str(tmp_path) not in json.dumps(expected)


@pytest.mark.parametrize("kind", ["other_workspace", "automatic_resume", "invalid_json",
                                  "too_large", "bad_status", "redirect"])
def test_probe_rejects_foreign_invalid_and_unbounded_health(tmp_path, monkeypatch, kind):
    expected = service_identity(tmp_path / "data")
    actual = dict(expected)
    status = 200
    if kind == "other_workspace":
        actual["workspace"] = "0" * 64
    if kind == "automatic_resume":
        actual["automatic_run_resume"] = True
    raw = json.dumps(actual).encode()
    if kind == "invalid_json":
        raw = b"not JSON"
    if kind == "too_large":
        raw = b" " * (launcher.HEALTH_BYTES + 1)
    if kind == "bad_status":
        status = 503
    if kind == "redirect":
        _opener(monkeypatch, error=urllib.error.HTTPError(
            "http://127.0.0.1:8765/health", 302, "redirect", {}, None))
    else:
        _opener(monkeypatch, _response(raw, status=status))
    with pytest.raises(launcher.LaunchError):
        launcher._probe(8765, expected)


def test_only_connection_refused_means_no_service(tmp_path, monkeypatch):
    expected = service_identity(tmp_path)
    _opener(monkeypatch, error=urllib.error.URLError(ConnectionRefusedError()))
    assert launcher._probe(8765, expected) is False
    _opener(monkeypatch, error=urllib.error.URLError(TimeoutError()))
    with pytest.raises(launcher.LaunchError):
        launcher._probe(8765, expected)


def test_existing_service_is_reused_without_spawning(launch_env, monkeypatch):
    path, _, _ = launch_env
    monkeypatch.setattr(launcher, "_probe", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "_spawn", lambda *a: pytest.fail("duplicate service"))
    result = launcher.ensure_server(path, 8765)
    assert result["started"] is False and result["url"] == "http://127.0.0.1:8765"


def test_concurrent_openers_share_one_server_start(launch_env, monkeypatch):
    path, _, _ = launch_env
    ready, starts = [], []
    monkeypatch.setattr(launcher, "_probe", lambda *a, **k: bool(ready))

    def spawn(*args):
        starts.append(args)
        ready.append(True)
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(launcher, "_spawn", spawn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: launcher.ensure_server(path, 8765), range(2)))
    assert len(starts) == 1
    assert sorted(result["started"] for result in results) == [False, True]


def test_spawn_uses_windowless_python_and_literal_arguments(launch_env, tmp_path, monkeypatch):
    config_path, _, log_dir = launch_env
    project = tmp_path / "project with spaces"
    pythonw = project / ".venv/Scripts/pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(launcher, "PROJECT", project)
    calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)) or "process")
    assert launcher._spawn(config_path, 8765, log_dir) == "process"
    positional, options = calls[0]
    assert positional[0][0] == str(pythonw)
    assert positional[0][2:] == ["--serve", "--config", str(config_path), "--port", "8765"]
    assert options["cwd"] == project and options["shell"] is False
    assert options["stdin"] == launcher.subprocess.DEVNULL
    assert options["stderr"] == launcher.subprocess.STDOUT and options["close_fds"]
    assert options["creationflags"] == getattr(launcher.subprocess, "CREATE_NO_WINDOW", 0)


def test_missing_windowless_runtime_fails_without_an_alternate_process(launch_env,
                                                                     tmp_path, monkeypatch):
    config_path, _, log_dir = launch_env
    monkeypatch.setattr(launcher, "PROJECT", tmp_path / "missing-runtime")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("fallback process"))
    with pytest.raises(launcher.LaunchError, match="Python"):
        launcher._spawn(config_path, 8765, log_dir)


def test_new_service_waits_for_readiness_before_returning(launch_env, monkeypatch):
    path, _, _ = launch_env
    states = iter([False, False, True])
    calls = []
    monkeypatch.setattr(launcher, "_probe", lambda *a, **k: next(states))
    monkeypatch.setattr(launcher, "_spawn", lambda *a: calls.append(a) or
                        SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    assert launcher.ensure_server(path, 8765)["started"] is True
    assert len(calls) == 1


def test_exited_duplicate_child_still_waits_for_another_starting_server(launch_env, monkeypatch):
    path, _, _ = launch_env
    states = iter([False, False, True])
    starts = []
    monkeypatch.setattr(launcher, "_probe", lambda *a, **k: next(states))
    monkeypatch.setattr(launcher, "_spawn", lambda *a: starts.append(a) or
                        SimpleNamespace(poll=lambda: 1))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    assert launcher.ensure_server(path, 8765)["url"] == "http://127.0.0.1:8765"
    assert len(starts) == 1


def test_readiness_deadline_does_not_spawn_again_or_terminate_a_process(launch_env, monkeypatch):
    path, _, _ = launch_env
    now, starts, probes = [0.0], [], []
    monkeypatch.setattr(launcher.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(launcher.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(launcher, "_probe", lambda *a, **k: probes.append(k) or False)
    monkeypatch.setattr(launcher, "_spawn", lambda *a: starts.append(a) or
                        SimpleNamespace(poll=lambda: None))
    with pytest.raises(launcher.LaunchError, match="尚未就绪"):
        launcher.ensure_server(path, 8765, timeout=.3)
    assert len(starts) == 1 and 2 <= len(probes) <= 4
    assert .3 <= now[0] <= .45


def test_not_ready_never_opens_browser(launch_env, monkeypatch):
    path, _, _ = launch_env
    monkeypatch.setattr(launcher, "ensure_server", lambda *a, **k: (_ for _ in ()).throw(
        launcher.LaunchError("not ready")))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda *a, **k: pytest.fail("opened browser"))
    monkeypatch.setattr(launcher, "_notify_error", lambda _: None)
    assert launcher.main(["--open", "--config", str(path)]) == 1


def test_open_and_startup_modes_only_perform_their_selected_action(launch_env, monkeypatch):
    path, _, _ = launch_env
    seen = []
    monkeypatch.setattr(launcher, "ensure_server", lambda *a: {
        "url": "http://127.0.0.1:8765", "started": False,
    })
    monkeypatch.setattr(launcher.webbrowser, "open", lambda *a, **k: seen.append((a, k)) or True)
    assert launcher.main(["--open", "--config", str(path)]) == 0
    assert seen == [(("http://127.0.0.1:8765",), {"new": 2})]
    seen.clear()
    assert launcher.main(["--ensure", "--config", str(path)]) == 0 and not seen
    monkeypatch.setattr(launcher, "serve_background", lambda *a: seen.append(a))
    assert launcher.main(["--serve", "--config", str(path)]) == 0
    assert seen == [(path.resolve(), 8765)]


def test_pythonw_binds_real_log_streams_before_uvicorn_setup(launch_env, monkeypatch):
    path, _, log_dir = launch_env
    monkeypatch.setattr(launcher, "_probe", lambda *a, **k: False)
    seen = []

    def serve(config, *, port):
        assert sys.stdout is not None and sys.stderr is not None
        assert sys.stdout is sys.stderr
        seen.append(port)
        print("windowless startup log")

    monkeypatch.setattr(launcher, "serve", serve)
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", None)
        context.setattr(sys, "stderr", None)
        launcher.serve_background(path, 8765)
        assert sys.stdout is None and sys.stderr is None
    assert seen == [8765]
    assert "windowless startup log" in (log_dir / "web.log").read_text(encoding="utf-8")


def test_app_health_never_resumes_saved_runs_and_retains_loopback_boundary(tmp_path, monkeypatch):
    store, config = text_environment(tmp_path)
    run = entrypoints.create_text(store, config, "Calculate water energy", submission_id="saved")[0]
    before = store.path(f"runs/{run.id}/run.json").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("web startup resumed a saved Run")

    monkeypatch.setattr(Coordinator, "start", forbidden)
    monkeypatch.setattr(entrypoints, "execute", forbidden)
    with TestClient(create_app(config, store=store), base_url="http://127.0.0.1:8765") as client:
        response = client.get("/health")
        assert response.status_code == 200 and response.json() == service_identity(store.root)
        assert client.get("/api/runs").status_code == 403
        assert client.get("/health", headers={"Host": "other.example"}).status_code == 403
        assert client.get("/health", headers={"Origin": "https://other.example"}).status_code == 403
        assert not client.app.state.coordinator.threads
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
