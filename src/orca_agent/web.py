"""Foreground loopback UI over the existing coordinator, never an alternate Agent."""

import asyncio
import json
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from filelock import FileLock, Timeout
from pydantic import Field

from orca_agent import entrypoints
from orca_agent.models import Identifier, Record, utc_now
from orca_agent.report import _safe
from orca_agent.store import Store, StoreError

STATIC = Path(__file__).with_name("static")


class Submission(Record):
    text: Annotated[str, Field(strict=True, min_length=1, max_length=8192)]
    submission_id: Identifier


class PresetSubmission(Record):
    submission_id: Identifier


class Message(Record):
    text: Annotated[str, Field(strict=True, min_length=1, max_length=8192)]
    message_id: Identifier


class EvidenceQuery(Record):
    tool: Literal["evidence.list", "evidence.discover", "evidence.value", "evidence.field",
                  "evidence.text", "evidence.search"]
    parameters: dict = Field(default_factory=dict)


class Coordinator:
    """Local thread ownership only; durable Run and environment locks stay in Store."""

    def __init__(self, store, config):
        self.store, self.config = store, config
        self.lock = threading.RLock()
        self.threads = {}
        self.closing = False
        self.stop_event = threading.Event()

    def active(self, run_id):
        with self.lock:
            return run_id in self.threads

    def start(self, run_id, *, resume):
        with self.lock:
            if self.closing:
                raise StoreError("service is shutting down")
            if run_id in self.threads:
                return False
            if self.threads:
                raise StoreError("another Run is active; no hidden queue is created")
            run = self.store.load_run(run_id)
            if run.batch_category is not None:
                raise StoreError("acceptance Runs require their original batch driver and ledger")
            # This early check gives CLI ownership conflicts a useful HTTP error;
            # the worker holds the same lock again for the whole actual execution.
            with self.store.run_lock(run_id):
                pass
            thread = threading.Thread(target=self._work, args=(run_id, resume), daemon=True,
                                      name="orca-web-coordinator")
            self.threads[run_id] = thread
            try:
                thread.start()
            except BaseException:
                self.threads.pop(run_id, None)
                raise
            return True

    def _work(self, run_id, resume):
        record = {"run_id": run_id, "started_at": utc_now().isoformat(), "status": "running"}
        try:
            with self.store.run_lock(run_id):
                # Shutdown can arrive between HTTP acceptance and thread start.
                with self.lock:
                    if self.closing:
                        record["status"] = "not_started"
                        return
                    self.store._write_json(f"web-operations/{run_id}.json", record)
                entrypoints.execute(self.store, self.config, run_id, resume=resume,
                                    stop_event=self.stop_event)
                record["status"] = "returned"
        except Exception as exc:
            record.update(status="failed", error=type(exc).__name__,
                          message="协调者未能完成；请查看状态，排除冲突后显式继续。")
        finally:
            record["finished_at"] = utc_now().isoformat()
            try:
                self.store._write_json(f"web-operations/{run_id}.json", record)
            finally:
                with self.lock:
                    self.threads.pop(run_id, None)

    def close(self, *, timeout=None):
        with self.lock:
            self.closing = True
            self.stop_event.set()
            threads = list(self.threads.items())
            remaining = 0.0
            for run_id, _ in threads:
                # Never downgrade an already-requested cancellation to pause.
                if self.store.read_signal(run_id) != "cancel":
                    self.store.signal(run_id, "pause")
                run = self.store.load_run(run_id)
                remaining = max(remaining, (run.deadline - utc_now()).total_seconds())
        end = time.monotonic() + (min(1830, max(30, remaining + 30)) if timeout is None else timeout)
        for _, thread in threads:
            thread.join(max(0, end - time.monotonic()))
        unfinished = [run_id for run_id, thread in threads if thread.is_alive()]
        if unfinished:
            self.store._write_json("web-shutdown.json", {
                "status": "unconfirmed", "run_ids": unfinished, "time": utc_now().isoformat(),
                "instruction": "Explicit resume must reconcile; resource occupancy is not released here."})
        return unfinished


def create_app(config, *, store=None, port=8765):
    store = store or Store(config.data_root)
    coordinator = Coordinator(store, config)
    token = secrets.token_urlsafe(32)
    origin = f"http://127.0.0.1:{port}"
    service_lock = FileLock(str(store.path("web-service.lock")), timeout=0)

    @asynccontextmanager
    async def lifespan(app):
        with service_lock:
            try:
                yield
            finally:
                await asyncio.to_thread(coordinator.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.coordinator = coordinator
    app.state.store = store

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.headers.get("host") != f"127.0.0.1:{port}":
            return JSONResponse({"error": "invalid_local_host"}, status_code=403)
        if (request.headers.get("sec-fetch-site") == "cross-site"
                or request.headers.get("origin") not in (None, origin)):
            return JSONResponse({"error": "same_origin_required"}, status_code=403)
        is_api = request.url.path.startswith("/api/")
        if is_api and request.url.path != "/api/bootstrap":
            if not secrets.compare_digest(request.headers.get("x-local-token", ""), token):
                return JSONResponse({"error": "local_token_required"}, status_code=403)
        if request.method != "GET":
            if request.method != "POST" or request.headers.get("origin") != origin:
                return JSONResponse({"error": "same_origin_required"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "json_required"}, status_code=415)
            # Count streamed bytes, not just an untrusted Content-Length header.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                                       "base-uri 'none'; form-action 'self'"})
        return response

    async def invalid(request, exc):
        return JSONResponse({"error": type(exc).__name__,
            "message": "请求不符合当前范围，或任务/文件不可用。请检查设置与状态后重试。"}, status_code=409)

    for error in (StoreError, ValueError, OSError, RuntimeError, Timeout, RequestValidationError):
        app.add_exception_handler(error, invalid)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    def static(name: str):
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/api/bootstrap")
    def bootstrap():
        import os
        return {"token": token, "text_enabled": config.text.enabled,
                "model_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
                "defaults": config.text.defaults,
                "permission": config.text.permission.model_dump(mode="json"),
                "budget": config.text.budget.model_dump(mode="json"),
                "scope": "H₂O / CH₄ · RHF/STO-3G · SP / 严格优化",
                "limitations": ["偶极矩合格输出、普通知识问答与联网检索尚未开放。",
                                "水分子自然语言单点计算已完成真实链路验证；其他任务覆盖范围仍有限。"]}

    @app.get("/api/runs")
    def runs(offset: int = 0, limit: int = 40):
        if not 0 <= offset <= 10000 or not 1 <= limit <= 100:
            raise StoreError("run page outside bounds")
        paths = sorted(store.path("runs").glob("*/run.json"),
                       key=lambda path: path.stat().st_mtime_ns, reverse=True)
        items = []
        for path in paths[offset:offset + limit]:
            try:
                run = store.load_run(path.parent.name)
                text = store.load_request(run).original_text
                items.append({"run_id": run.id, "state": run.state,
                              "text": _safe(text[:120]), "created_at": run.created_at.isoformat()})
            except (ValueError, OSError, RuntimeError):
                items.append({"run_id": path.parent.name, "state": "unreadable", "text": "记录不可读"})
        return {"items": items, "total": len(paths),
                "next_offset": offset + len(items) if offset + len(items) < len(paths) else None}

    @app.post("/api/runs", status_code=202)
    def submit(body: Submission):
        with coordinator.lock:
            if coordinator.closing:
                raise StoreError("service is shutting down")
            run, created = entrypoints.create_text(store, config, body.text,
                                                   submission_id=body.submission_id)
            # Replayed HTTP POST never resumes a paused/failed/completed Run.
            try:
                started = coordinator.start(run.id, resume=False) if created else False
            except (StoreError, Timeout):
                return {"run_id": run.id, "created": created, "started": False,
                        "start_error": "任务已保存；当前协调者忙碌。请稍后显式继续。"}
            return {"run_id": run.id, "created": created, "started": started}

    @app.post("/api/presets/water-sp", status_code=202)
    def water_sp(body: PresetSubmission):
        with coordinator.lock:
            if coordinator.closing:
                raise StoreError("service is shutting down")
            run, created = entrypoints.create_water_sp(store, config,
                                                       submission_id=body.submission_id)
            try:
                started = coordinator.start(run.id, resume=False) if created else False
            except (StoreError, Timeout):
                return {"run_id": run.id, "created": created, "started": False,
                        "start_error": "任务已保存；当前协调者忙碌。请稍后显式继续。"}
            return {"run_id": run.id, "created": created, "started": started}

    @app.get("/api/runs/{run_id}")
    def state(run_id: str):
        data = entrypoints.status(store, run_id)
        data["coordinator_active"] = coordinator.active(run_id)
        path = f"web-operations/{run_id}.json"
        data["web_operation"] = store._read_json(path) if store.path(path).exists() else None
        return data

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str):
        return entrypoints.report(store, run_id)

    @app.get("/api/runs/{run_id}/messages")
    def history(run_id: str, offset: int = 0, limit: int = 40):
        return entrypoints.messages(store, run_id, offset=offset, limit=limit)

    @app.post("/api/runs/{run_id}/messages", status_code=202)
    def message(run_id: str, body: Message):
        return entrypoints.message(store, run_id, body.text, message_id=body.message_id)

    @app.post("/api/runs/{run_id}/control", status_code=202)
    def control(run_id: str, body: dict):
        if set(body) != {"action"}:
            raise StoreError("control only accepts an action")
        if body["action"] == "resume":
            return {"run_id": run_id, "started": coordinator.start(run_id, resume=True)}
        return entrypoints.control(store, run_id, body["action"])

    @app.post("/api/runs/{run_id}/evidence")
    def evidence(run_id: str, body: EvidenceQuery):
        return _safe(entrypoints.evidence(store, run_id, body.tool, body.parameters), bounded=False)

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str, offset: int = 0, limit: int = 40):
        return _safe(entrypoints.evidence(store, run_id, "evidence.list",
                                          {"offset": offset, "limit": limit}), bounded=False)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}/download")
    def download(run_id: str, artifact_id: str):
        content, _, digest = entrypoints.download(store, run_id, artifact_id)
        # Artifact ID is controlled ASCII; the untrusted source filename is not a header.
        return Response(content, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{artifact_id}.bin"',
            "X-Artifact-SHA256": digest})

    return app


def serve(config, *, port=8765):
    import uvicorn
    if not 1024 <= port <= 65535:
        raise StoreError("port must be between 1024 and 65535")
    print(json.dumps({"url": f"http://127.0.0.1:{port}", "mode": "foreground",
                      "restart": "no automatic resume"}, ensure_ascii=False), flush=True)
    uvicorn.run(create_app(config, port=port), host="127.0.0.1", port=port, workers=1,
                access_log=False, proxy_headers=False, timeout_graceful_shutdown=5)
