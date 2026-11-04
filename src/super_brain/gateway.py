from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

from fastapi import Request

from .models import Backend, new_id
from .policy import budget_reason
from .store import Conflict
from .worker import worker_token


class ManagedGateway:
    def __init__(self, manager, secret: str):
        self.manager = manager
        self.secret = secret.encode()
        self.waiters: list[tuple[str, set[str]]] = []

    def token(self, task_id: str) -> str:
        encoded = base64.urlsafe_b64encode(task_id.encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
        return f"sbt.{encoded}.{signature}"

    def worker_token(self, host_id: str) -> str:
        return worker_token(self.secret.decode(), host_id)

    def authenticate(self, request: Request) -> str:
        value = request.headers.get("x-super-brain-task-token") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not value:
            # Codex 的 thread-id 由本机协议实测确认；缺少身份时不按目录或模型猜测任务。
            worker = request.headers.get("x-super-brain-worker-token", "")
            thread_id = request.headers.get("thread-id")
            if request.headers.get("session-id", thread_id) != thread_id:
                raise Conflict("执行端请求的会话身份不一致")
            for host in self.manager.store.list("host"):
                if host["enabled"] and hmac.compare_digest(worker.encode(), self.worker_token(host["id"]).encode()):
                    session = self.manager.store.get("session", f"{host['id']}:{thread_id}") if thread_id else None
                    if (session and session.get("task_id") and session.get("budget_ready")
                        and session.get("connected") and session.get("budget_accounting") == "gateway"
                        and session.get("binding_source") in {"managed", "inherited"}):
                        return session["task_id"]
                    break
            raise Conflict("执行端会话尚未核对受管任务身份，推理未发送")
        try:
            prefix, encoded, signature = value.split(".")
            expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).hexdigest()
            if prefix != "sbt" or not hmac.compare_digest(expected, signature):
                raise ValueError
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        except (ValueError, UnicodeError) as exc:
            raise Conflict("受管推理请求缺少有效的任务授权") from exc

    async def before(self, request: Request, body: dict) -> str:
        manager = self.manager
        task_id = self.authenticate(request)
        task = manager.task(task_id)
        if reason := budget_reason(task, manager.clock()):
            raise Conflict(reason)
        route = manager.route(body["model"])
        if not route.enabled:
            raise Conflict("模型入口已禁用")
        if route.purpose == "recovery":
            # 原生/人工压缩不受管理器的停滞触发条件限制，但仍遵守同一任务预算。
            # HTTP 请求可能先于 SSH 事件到达，短暂等待已开始的原生压缩记录。
            deadline = asyncio.get_running_loop().time() + 1
            allowed = False
            while True:
                for record in manager.store.list("recovery"):
                    if record["task_id"] != task_id:
                        continue
                    session = manager.session(record["session_id"])
                    if record["state"] == "compressing" or (
                        record["state"] == "reconcile_required" and session.connected and record.get("compaction_turn_id")
                        and record["compaction_turn_id"] == session.active_turn_id and session.model == record.get("compression_model")):
                        allowed = True
                        break
                native = any(session["task_id"] == task_id and session["connected"] and session["model"] == route.name
                    and session.get("compaction_turn_id") and session.get("compaction_turn_id") == session.get("active_turn_id")
                    for session in manager.store.list("session"))
                if allowed or native or asyncio.get_running_loop().time() >= deadline:
                    allowed = allowed or native
                    break
                if await request.is_disconnected():
                    raise asyncio.CancelledError
                await asyncio.sleep(.02)
            if not allowed:
                raise Conflict("恢复入口需要已授权的恢复事务或实际原生压缩回合")
        elif task.allowed_models and route.name not in task.allowed_models:
            raise Conflict("模型超出任务允许范围")
        limits = {}
        for key in route.backend_ids:
            backend = Backend.model_validate(manager.store.require("backend", key))
            external = max(0, int(backend.metrics.get("external_running", 0)))
            limits[key] = max(0, backend.max_requests - external)
        estimated = max(25000, len(json.dumps(body, ensure_ascii=False).encode()) // 2 + int(body.get("max_output_tokens", 4096)))
        request_id = new_id()
        queued = False
        self.waiters.append((request_id, set(limits)))
        try:
            async with asyncio.timeout(manager.config.inference.queue_timeout):
                while True:
                    earlier = [keys for key, keys in self.waiters[:next(i for i, item in enumerate(self.waiters) if item[0] == request_id)]]
                    overlaps = any(set(limits) & keys for keys in earlier)
                    if not overlaps and manager.store.begin_inference(request_id, task_id, body["model"], limits, estimated, manager.config.inference.max_concurrent_requests):
                        return request_id
                    if await request.is_disconnected():
                        raise asyncio.CancelledError
                    if not queued:
                        manager.store.event("inference.queued", task_id, {"model": body["model"], "reason": "等待共享后端请求名额"})
                        queued = True
                    await asyncio.sleep(0.05)
        finally:
            self.waiters = [item for item in self.waiters if item[0] != request_id]

    async def after(self, request_id: str, usage: dict | None, state: str) -> None:
        used = None
        if usage:
            used = usage.get("total_tokens")
            if used is None and "input_tokens" in usage and "output_tokens" in usage:
                used = usage["input_tokens"] + usage["output_tokens"]
        self.manager.store.finish_inference(request_id, used, state)
