from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .gateway import ManagedGateway
from .inference_proxy import inference_proxy
from .manager import Manager
from .mcp_server import build_mcp
from .models import Host, ServiceConfig, StallPolicy, Task, new_id, public_recovery
from .store import Conflict, NotFound
from .usage import UsageLedger


class Command(BaseModel):
    action: str
    payload: dict = Field(default_factory=dict)
    operation_id: str = Field(default_factory=new_id)


def create_app(config: ServiceConfig | None = None, manager: Manager | None = None) -> FastAPI:
    config = config or ServiceConfig()
    manager = manager or Manager(config)
    token = os.environ.get(config.token_env)
    mcp = build_mcp(manager)
    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=config.allowed_hosts))
    inference_app = None
    ledger = None
    if config.inference.enabled:
        if not token or not config.inference.upstream_url:
            raise ValueError("启用受管推理入口需要管理令牌和明确的 upstream_url")
        manager.gateway = ManagedGateway(manager, token)
        ledger = UsageLedger(config.inference.ledger_path, config.inference.total_token_limit)
        inference_app = inference_proxy(config.inference.upstream_url,
            os.environ.get(config.inference.api_key_env), ledger,
            {row["name"]: row.get("upstream_model") or row["name"] for row in manager.store.list("route")},
            before=manager.gateway.before, after=manager.gateway.after,
            max_output_tokens=config.inference.max_output_tokens)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp.session_manager.run())
            if inference_app:
                await stack.enter_async_context(inference_app.router.lifespan_context(inference_app))
                ledger.reconcile_abandoned()
            await manager.start()
            try:
                yield
            finally:
                await manager.stop()
                if ledger:
                    ledger.close()

    app = FastAPI(title="Super Brain", version="0.1.0", lifespan=lifespan)
    app.state.manager, app.state.mcp = manager, mcp

    @app.middleware("http")
    async def access(request: Request, call_next):
        protected = request.url.path.startswith(("/api/", "/mcp")) and request.url.path != "/api/login"
        if token and protected:
            authorization = request.headers.get("authorization", "")
            bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
            cookie = request.cookies.get("super_brain_token", "")
            if not (secrets.compare_digest(bearer, token) or secrets.compare_digest(cookie, token)):
                return JSONResponse({"detail": "需要管理服务访问令牌"}, status_code=401)
        origin = request.headers.get("origin")
        if protected and request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "拒绝来自其他站点的控制操作"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(NotFound)
    async def not_found_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/api/login")
    async def login(request: Request):
        data = await request.json()
        if token and not secrets.compare_digest(str(data.get("token", "")), token):
            raise HTTPException(401, "令牌不正确")
        response = JSONResponse({"ok": True})
        if token:
            response.set_cookie("super_brain_token", token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    @app.get("/api/snapshot")
    def snapshot():
        return manager.snapshot()

    @app.get("/api/report")
    def report():
        return {"text": manager.report(), "cursor": manager.store.cursor()}

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 100, entity_id: str | None = None):
        return manager.store.events(after, limit, entity_id)

    @app.get("/api/sessions/{session_id}/recent")
    def recent(session_id: str, limit: int = 100):
        manager.session(session_id)
        return manager.store.recent_events(session_id, limit)

    @app.get("/api/stream")
    async def stream(request: Request, after: int = 0):
        async def generate():
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            while not await request.is_disconnected():
                rows = manager.store.events(cursor, 200)
                if rows:
                    for row in rows:
                        cursor = row["seq"]
                        yield f"id: {cursor}\nevent: update\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                else:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/tasks")
    def task_create(task: Task):
        return manager.create_task(task)

    @app.patch("/api/tasks/{task_id}")
    async def task_update(task_id: str, request: Request, expected: int | None = None):
        return manager.update_task(task_id, await request.json(), actor="user", expected=expected)

    @app.post("/api/tasks/{task_id}/verify")
    async def task_verify(task_id: str, request: Request):
        data = await request.json()
        return manager.verify_task(task_id, bool(data.get("passed")), data.get("evidence", ""))

    @app.post("/api/tasks/{task_id}/dispatch")
    async def dispatch(task_id: str):
        return await manager.dispatch(task_id)

    @app.post("/api/proposals/{proposal_id}/decide")
    async def decide(proposal_id: str, request: Request):
        proposal = manager.store.require("proposal", proposal_id)
        if proposal["state"] != "pending":
            raise Conflict("提案已处理")
        approved = (await request.json()).get("approved") is True
        if approved:
            manager.update_task(proposal["task_id"], proposal["patch"], actor="user")
        proposal["state"] = "approved" if approved else "rejected"
        return manager.save("proposal", proposal)

    @app.post("/api/hosts")
    async def add_host(host: Host):
        if manager.store.get("host", host.id):
            raise Conflict("主机 ID 已存在")
        result = manager.save("host", host, expected=0)
        if host.enabled:
            manager.jobs.append(asyncio.create_task(manager._host_loop(host.id)))
        return result

    @app.post("/api/sessions/{session_id}/bind")
    async def bind(session_id: str, request: Request):
        data = await request.json()
        return manager.bind_session(session_id, data["task_id"], data.get("control_enabled", False))

    @app.post("/api/sessions/{session_id}/command")
    async def command(session_id: str, data: Command):
        return await manager.command(session_id, data.action, data.payload, data.operation_id, actor="user")

    @app.post("/api/sessions/{session_id}/policy")
    def policy(session_id: str, data: StallPolicy):
        session = manager.session(session_id)
        session.stall_policy = data
        session.binding_source = "manual"
        session.control_revision += 1
        return manager.save("session", session, expected=session.revision)

    @app.post("/api/manager-action")
    async def manager_action(request: Request):
        data = await request.json()
        tool, args = data.get("tool"), data.get("arguments", {})
        sync_tools = {"task_create": lambda: manager.create_task(Task.model_validate(args)),
                      "task_update": lambda: manager.update_task(args["task_id"], args["patch"], expected=args.get("expected_revision")),
                      "task_verify": lambda: manager.verify_task(args["task_id"], args["passed"], args["evidence"], event_ids=args.get("evidence_event_ids"), actor="manager"),
                      "session_bind": lambda: manager.bind_session(**args),
                      "session_diagnose": lambda: manager.diagnose(**args),
                      "session_progress": lambda: manager.mark_progress(**args)}
        if tool in sync_tools:
            return sync_tools[tool]()
        if tool == "task_dispatch":
            return await manager.dispatch(args["task_id"])
        if tool == "session_message":
            return await manager.command(args["session_id"], "message", {"text": args["text"]}, args["operation_id"], expected_control_revision=args.get("expected_control_revision"))
        if tool == "session_interrupt":
            return await manager.command(args["session_id"], "interrupt", {"turn_id": args["expected_turn_id"]}, args["operation_id"], expected_control_revision=args.get("expected_control_revision"))
        if tool == "session_recover":
            return public_recovery(await manager.recover(**args))
        if tool == "recovery_reconcile":
            return public_recovery(await manager.reconcile_recovery(args["recovery_id"]))
        raise HTTPException(400, "不支持的管理工具")

    static = Path(__file__).parent / "static"
    if inference_app:
        app.mount("/inference", inference_app)
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/")
    def index():
        if (static / "index.html").is_file():
            return FileResponse(static / "index.html")
        return HTMLResponse("<html lang='zh-CN'><title>Super Brain</title><h1>Super Brain</h1><p>服务已启动，请先构建前端。API：/api/snapshot；MCP：/mcp</p></html>")

    # SDK 子应用的 lifespan 已由宿主 lifespan 显式启动。
    app.mount("/", mcp_app)
    return app
