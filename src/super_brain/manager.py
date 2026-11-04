from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

import httpx

from .metrics import parse_metrics
from .models import (
    Backend,
    Budget,
    Host,
    ModelRoute,
    Recovery,
    ServiceConfig,
    Session,
    Task,
    new_id,
    public_recovery,
)
from .policy import assess_stall, budget_reason, task_scope_fingerprint, within_scope
from .probe import check_remote_scope, probe_host
from .rpc import CodexClient, RpcError
from .store import Conflict, Store
from .worker import verify_worker_config


class Manager:
    def __init__(self, config: ServiceConfig, store: Store | None = None,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.store = store or Store(config.database)
        self.clock = clock
        self.clients: dict[str, CodexClient] = {}
        self.host_ready: dict[str, asyncio.Event] = {}
        self.jobs: list[asyncio.Task] = []
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.task_locks: dict[str, asyncio.Lock] = {}
        self._stopping = False
        self.manager_message_ids: set[str] = set()
        self.gateway = None
        self.worker_verified: set[str] = set()
        self.expected_models: dict[str, list[tuple[str, float]]] = {}
        self.expected_interrupts: set[tuple[str, str]] = set()
        self.background_controls: dict[str, asyncio.Task] = {}
        for kind, values in [("host", config.hosts), ("backend", config.backends), ("route", config.routes)]:
            for value in values:
                old = self.store.get(kind, value.id)
                if not old:
                    self.save(kind, value)
                else:
                    runtime = {"id", "revision", "created_at", "updated_at", "connection", "last_error", "capabilities", "metrics", "observed_at"}
                    declared = value.model_dump(include=value.model_fields_set - runtime, mode="json")
                    if any(old.get(key) != val for key, val in declared.items()):
                        self.save(kind, {**old, **declared}, expected=old["revision"])

    def save(self, kind: str, value: Any, *, expected: int | None = None) -> dict:
        body = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        return self.store.put(kind, body, expected)

    def task(self, task_id: str) -> Task:
        return Task.model_validate(self.store.require("task", task_id))

    def session(self, session_id: str) -> Session:
        return Session.model_validate(self.store.require("session", session_id))

    def _inherit_agent_task(self, session: Session, thread: dict) -> None:
        source = thread.get("source")
        fallback = source.get("subAgent", {}).get("thread_spawn", {}).get("parent_thread_id") if isinstance(source, dict) and isinstance(source.get("subAgent"), dict) else None
        session.parent_thread_id = thread.get("parentThreadId") or fallback or session.parent_thread_id
        session.agent_nickname = thread.get("agentNickname", session.agent_nickname)
        session.agent_role = thread.get("agentRole", session.agent_role)
        if session.parent_thread_id:
            parent = self.store.get("session", f"{session.host_id}:{session.parent_thread_id}")
            if parent and parent.get("task_id") and session.binding_source in {"discovered", "inherited"} and (not session.task_id or session.task_id == parent["task_id"]):
                session.task_id = parent["task_id"]
                session.control_enabled = parent["control_enabled"]
                session.budget_accounting = parent.get("budget_accounting", "native_estimate")
                session.budget_ready = parent.get("budget_ready", False)
                if session.binding_source == "discovered":
                    session.stall_policy = Session.model_validate(parent).stall_policy.model_copy()
                session.binding_source = "inherited"

    def create_task(self, task: Task) -> dict:
        if self.store.get("task", task.id):
            raise Conflict("任务 ID 已存在")
        if all(limit is None for limit in (task.budget.max_requests, task.budget.max_tokens, task.budget.max_seconds)):
            task.budget = Budget.model_validate({**self.config.default_task_budget.model_dump(),
                **task.budget.model_dump(include=task.budget.model_fields_set)})
        for dependency in task.dependencies:
            if dependency == task.id:
                raise Conflict("任务不能依赖自身")
            self.store.require("task", dependency)
        if task.parent_id:
            self.store.require("task", task.parent_id)
        if task.cwd and not within_scope(task.cwd, task.allowed_roots):
            raise Conflict("任务工作目录不在允许范围内")
        if task.host_id:
            host = Host.model_validate(self.store.require("host", task.host_id))
            if task.cwd and not within_scope(task.cwd, host.allowed_roots):
                raise Conflict("工作目录不在主机配置允许范围内")
            if any(not within_scope(root, host.allowed_roots) for root in task.allowed_roots):
                raise Conflict("任务允许目录扩大了主机授权范围")
        with self.store.transaction():
            result = self.save("task", task, expected=0)
            self.invalidate_parent_verification(task.parent_id)
            return result

    def invalidate_parent_verification(self, task_id: str | None) -> None:
        visited = set()
        while task_id:
            if task_id in visited:
                raise Conflict("父子任务关系形成环")
            visited.add(task_id)
            parent = self.task(task_id)
            if (parent.verification or {}).get("passed"):
                parent.verification = None
                if parent.status == "complete":
                    parent.status = "planned"
                self.save("task", parent, expected=parent.revision)
            task_id = parent.parent_id

    def update_task(self, task_id: str, patch: dict, *, actor: str = "manager",
                    expected: int | None = None) -> dict:
        with self.store.transaction():
            return self._update_task(task_id, patch, actor=actor, expected=expected)

    def _update_task(self, task_id: str, patch: dict, *, actor: str,
                     expected: int | None) -> dict:
        task = self.task(task_id)
        protected = {"objective", "parent_id", "dependencies", "host_id", "cwd", "allowed_roots", "allowed_models", "acceptance", "budget"}
        changed = {key for key, value in patch.items() if task.model_dump().get(key) != value}
        if actor != "user" and changed & protected:
            proposal = {"id": new_id(), "task_id": task_id, "patch": patch, "state": "pending",
                        "reason": "改变任务目标、范围或预算需要用户决定", "created_at": self.clock()}
            return {"approval_required": True, "proposal": self.save("proposal", proposal)}
        allowed = protected | {"title", "priority", "status", "blocked_reason", "next_step"}
        if set(patch) - allowed:
            raise Conflict("包含不能直接修改的任务字段")
        value = Task.model_validate({**task.model_dump(), **patch})
        scope_changed = bool(changed & {"objective", "dependencies", "host_id", "cwd", "allowed_roots", "allowed_models", "acceptance"})
        if scope_changed:
            value.verification = None
            value.verification_after_seq = self.store.cursor()
            if value.status == "complete":
                if patch.get("status") == "complete":
                    raise Conflict("任务范围变化后需要重新验收")
                value.status = "planned"
        if value.cwd and not within_scope(value.cwd, value.allowed_roots):
            raise Conflict("工作目录越出任务范围")
        if value.host_id:
            host = Host.model_validate(self.store.require("host", value.host_id))
            if any(not within_scope(root, host.allowed_roots) for root in value.allowed_roots):
                raise Conflict("任务允许目录扩大了主机授权范围")
        if value.status == "complete" and not (value.verification or {}).get("passed"):
            raise Conflict("任务尚无通过的验收证据")
        # 更新依赖时检查整个依赖图，避免跨多次编辑形成环。
        all_tasks = {row["id"]: row for row in self.store.list("task")}
        all_tasks[value.id] = value.model_dump()
        ancestor, parent_path = value.parent_id, {value.id}
        while ancestor:
            if ancestor in parent_path:
                raise Conflict("父子任务关系形成环")
            if ancestor not in all_tasks:
                raise Conflict("父任务不存在")
            parent_path.add(ancestor)
            ancestor = all_tasks[ancestor]["parent_id"]
        def visit(node: str, path: set[str]):
            if node in path:
                raise Conflict("任务依赖形成环")
            if node not in all_tasks:
                raise Conflict("依赖任务不存在")
            for dep in all_tasks[node]["dependencies"]:
                visit(dep, path | {node})
        visit(value.id, set())
        with self.store.transaction():
            result = self.save("task", value, expected=task.revision if expected is None else expected)
            if scope_changed or task.status != value.status:
                for saved in self.store.list("session"):
                    if saved["task_id"] == task_id:
                        session = Session.model_validate(saved)
                        session.control_revision += 1
                        session.diagnosis = None
                        self.save("session", session, expected=session.revision)
            if scope_changed or value.parent_id != task.parent_id or task.status == "complete" and value.status != "complete":
                self.invalidate_parent_verification(task.parent_id)
                if value.parent_id != task.parent_id:
                    self.invalidate_parent_verification(value.parent_id)
            return result

    def verify_task(self, task_id: str, passed: bool, evidence: str,
                    event_ids: list[int] | None = None, actor: str = "user") -> dict:
        with self.store.transaction():
            return self._verify_task(task_id, passed, evidence, event_ids, actor)

    def _verify_task(self, task_id: str, passed: bool, evidence: str,
                     event_ids: list[int] | None, actor: str) -> dict:
        if not evidence.strip():
            raise Conflict("必须提供验收证据")
        task = self.task(task_id)
        if actor != "user" and passed:
            all_tasks = {row["id"]: row for row in self.store.list("task")}
            relevant = {task_id}
            pending = [task_id]
            while pending:
                parent = pending.pop()
                for child in all_tasks.values():
                    if child["parent_id"] != parent:
                        continue
                    if child["id"] in relevant:
                        raise Conflict("父子任务关系形成环")
                    if child["status"] != "complete" or not (child.get("verification") or {}).get("passed"):
                        raise Conflict("仍有子任务尚未验收通过，不能自动完成父任务")
                    relevant.add(child["id"])
                    pending.append(child["id"])
            if any(all_tasks[dependency]["status"] != "complete" for dependency in task.dependencies):
                raise Conflict("前置任务尚未完成，不能自动验收")
            valid = False
            sessions = {session["id"] for session in self.store.list("session") if session["task_id"] in relevant}
            for event in self.store.events_by_ids(event_ids or []):
                item = event["body"].get("params", {}).get("item", {}) if event["kind"] == "codex.event" else {}
                if event["seq"] > task.verification_after_seq and event["entity_id"] in sessions and event["body"].get("method") == "item/completed" and item.get("type") == "commandExecution" and item.get("exitCode") == 0:
                    valid = True
            if not valid:
                raise Conflict("自动验收需要属于该任务的实际成功命令事件证据")
        task.verification = {"passed": passed, "evidence": evidence, "event_ids": event_ids or [], "source": actor, "at": self.clock()}
        task.status = "complete" if passed else "blocked"
        task.blocked_reason = None if passed else "验收未通过"
        result = self.save("task", task, expected=task.revision)
        if not passed:
            self.invalidate_parent_verification(task.parent_id)
        if not passed and actor != "user":
            from .supervisor import queue_decision
            for value in self.store.list("session"):
                if value["task_id"] == task.id and value["control_enabled"]:
                    queue_decision(self, Session.model_validate(value), "验收未通过：" + evidence,
                                   "verification_failed", self.clock() + 5)
        if passed and not any(s["task_id"] == task.id and s["status"] == "active" for s in self.store.list("session")):
            self.store.release_task(task.id)
        return result

    def route(self, name: str) -> ModelRoute:
        for value in self.store.list("route"):
            if value["name"] == name:
                return ModelRoute.model_validate(value)
        raise Conflict(f"没有配置模型入口 {name}")

    def select_route(self, task: Task) -> dict | None:
        if budget_reason(task, self.clock()):
            return None
        if any(self.task(dep).status != "complete" for dep in task.dependencies):
            return None
        allocations = self.store.allocations()
        candidates = []
        for value in self.store.list("route"):
            route = ModelRoute.model_validate(value)
            if not route.enabled or route.purpose != "execution" or route.opaque:
                continue
            if task.allowed_models and route.name not in task.allowed_models:
                continue
            backends = [Backend.model_validate(self.store.require("backend", key)) for key in route.backend_ids]
            if not backends:
                continue
            if not route.can_pin_backend and len(backends) > 1:
                # 无法指定落点时，对所有可能后端做保守准入，而非假装能选中空闲实例。
                if any(not self.backend_available(b) or sum(a["backend_id"] == b.id for a in allocations) >= b.max_sessions for b in backends):
                    continue
            for backend in backends:
                used = sum(a["backend_id"] == backend.id for a in allocations)
                if backend.opaque or not self.backend_available(backend) or used >= backend.max_sessions:
                    continue
                queue = backend.metrics.get("waiting", 0)
                reservations = [b.id for b in backends] if not route.can_pin_backend else [backend.id]
                candidates.append((queue + used, route.name, backend.id, reservations))
        if not candidates:
            return None
        _, model, backend_id, reservations = min(candidates)
        route = self.route(model)
        return {"model": model, "backend_id": backend_id if len(route.backend_ids) == 1 or route.can_pin_backend else None,
                "capacity_reservations": reservations, "backend_pin_required": route.can_pin_backend and len(route.backend_ids) > 1}

    def backend_available(self, backend: Backend) -> bool:
        return backend.health == "available" and (not backend.metrics_url or (
            backend.observed_at is not None and self.clock() - backend.observed_at <= max(30, self.config.poll_interval * 3)))

    async def dispatch(self, task_id: str) -> dict:
        async with self.task_locks.setdefault(task_id, asyncio.Lock()):
            return await self._dispatch(task_id)

    def require_dispatch_unchanged(self, task: Task) -> None:
        current = self.task(task.id)
        if current.revision != task.revision:
            raise Conflict("任务在分配期间发生变化，取消过期分配")
        if any(self.task(dependency).status != "complete" for dependency in current.dependencies):
            raise Conflict("前置任务尚未完成")

    def inference_endpoint(self, host: Host) -> str:
        if host.inference_url:
            return host.inference_url.rstrip("/")
        if host.gateway_remote_port:
            return f"http://127.0.0.1:{host.gateway_remote_port}/inference/v1"
        if self.config.inference.public_url:
            return self.config.inference.public_url.rstrip("/") + "/inference/v1"
        raise Conflict("需要配置执行主机可访问的受管推理地址或 SSH 反向转发端口")

    async def verify_worker(self, host: Host, client: CodexClient) -> str:
        if not self.gateway:
            raise Conflict("受管推理网关尚未启动")
        native = (await client.call("config/read", {"includeLayers": False})).get("config", {})
        provider = verify_worker_config(native, self.inference_endpoint(host), self.gateway.worker_token(host.id))
        self.worker_verified.add(host.id)
        current = self.store.require("host", host.id)
        current["capabilities"]["request_budget"] = {"verified": True}
        self.save("host", current)
        return provider

    def managed_overrides(self, task: Task, host: Host, provider_id: str) -> dict:
        return {
            "agents.enabled": task.budget.max_agents > 0,
            "agents.max_concurrent_threads_per_session": max(1, task.budget.max_agents),
            "model_provider": provider_id,
            f"model_providers.{provider_id}": {"name": "Super Brain", "base_url": self.inference_endpoint(host),
                "wire_api": "responses", "requires_openai_auth": False, "supports_websockets": False,
                "http_headers": {"X-Super-Brain-Task-Token": self.gateway.token(task.id),
                    "X-Super-Brain-Worker-Token": self.gateway.worker_token(host.id)},
                "request_max_retries": 0, "stream_max_retries": 0},
        }

    async def _dispatch(self, task_id: str) -> dict:
        task = self.task(task_id)
        if task.status not in {"planned", "ready", "waiting_resource"}:
            raise Conflict("任务当前状态不允许重新分配")
        if not task.host_id or not task.cwd or not within_scope(task.cwd, task.allowed_roots):
            raise Conflict("任务需要指定主机、工作目录和允许范围")
        host = Host.model_validate(self.store.require("host", task.host_id))
        if not within_scope(task.cwd, host.allowed_roots):
            raise Conflict("工作目录超出主机允许范围")
        client = self.clients.get(task.host_id)
        if not client or not client.connected:
            raise Conflict("执行主机尚未连接")
        if reason := budget_reason(task, self.clock()):
            raise Conflict(reason)
        if any(self.task(dep).status != "complete" for dep in task.dependencies):
            raise Conflict("前置任务尚未完成")
        selection = self.select_route(task)
        if not selection:
            task.status, task.blocked_reason = "waiting_resource", "没有满足预算与容量的可用模型入口"
            return self.save("task", task, expected=task.revision)
        if selection["backend_pin_required"]:
            raise Conflict("该路由需要部署固定能力，尚未配置实际固定方式")
        for roots in (task.allowed_roots, host.allowed_roots):
            checked = await check_remote_scope(host, task.cwd, roots)
            if not checked["allowed"]:
                raise Conflict("工作目录经远端符号链接解析后越出允许范围")
        self.require_dispatch_unchanged(task)
        allocation_id = new_id()
        limits = {key: self.store.require("backend", key)["max_sessions"] for key in selection["capacity_reservations"]}
        if not self.store.reserve_group(allocation_id, task.id, limits, task.budget.max_sessions):
            raise Conflict("资源已经被其他任务占用")
        sent = False
        session_id = None
        try:
            overrides = {"agents.enabled": task.budget.max_agents > 0,
                         "agents.max_concurrent_threads_per_session": max(1, task.budget.max_agents)}
            start_params = {"cwd": task.cwd, "model": selection["model"],
                "approvalPolicy": "never", "sandbox": "workspace-write",
                "config": overrides}
            if self.config.inference.enabled:
                provider_id = await self.verify_worker(host, client)
                start_params["config"] = self.managed_overrides(task, host, provider_id)
                start_params["modelProvider"] = provider_id
            self.require_dispatch_unchanged(task)
            result = await client.call("thread/start", start_params)
            thread = result["thread"]
            session_id = f"{task.host_id}:{thread['id']}"
            session = Session(id=session_id, host_id=task.host_id, thread_id=thread["id"],
                task_id=task.id, cwd=task.cwd, model=selection["model"], backend_id=selection["backend_id"],
                model_provider=result.get("modelProvider"), connected=True, control_enabled=True, status="idle")
            session.budget_accounting = "gateway" if self.config.inference.enabled else "native_estimate"
            session.budget_ready = self.config.inference.enabled
            session.binding_source = "managed"
            with self.store.transaction():
                self.require_dispatch_unchanged(task)
                self.save("session", session)
                self.store.attach_allocation(allocation_id, session_id)
                task.status, task.blocked_reason = "running", None
                task.started_at = task.started_at or self.clock()
                self.save("task", task, expected=task.revision)
            message_id = f"sb-dispatch-{allocation_id}"
            self.manager_message_ids.add(message_id)
            sent = True
            turn = await client.call("turn/start", {"threadId": session.thread_id,
                "clientUserMessageId": message_id,
                "runtimeWorkspaceRoots": task.allowed_roots,
                "input": [{"type": "text", "text": f"工作任务：{task.title}\n目标：{task.objective}\n工作目录：{task.cwd}\n验收要求：{task.acceptance}\n在既定目录和预算内完成工作，报告实际修改与验证证据。改变目标或扩大范围时先报告。"}]})
            return {"task": self.store.require("task", task.id), "session_id": session_id,
                    "turn": turn, "allocation_id": allocation_id,
                    "budget_enforcement": "受管网关请求准入及共享后端并发已启用，token 使用保守估算与实际上报" if self.config.inference.enabled else "会话准入已生效；请求级硬上限需要受管推理网关"}
        except BaseException:
            if not sent:
                self.store.release_group(allocation_id)
                if session_id:
                    self.store.event("dispatch.abandoned_thread", task_id,
                        {"session_id": session_id, "reason": "分配在工作指令发出前取消，未执行任务"})
            else:
                self.store.event("dispatch.reconcile_required", task_id, {"allocation_id": allocation_id})
            raise

    def bind_session(self, session_id: str, task_id: str, control_enabled: bool = False) -> dict:
        session = self.session(session_id)
        if self.session_locks.get(session_id) and self.session_locks[session_id].locked():
            raise Conflict("会话有正在执行的控制动作，请等待核对完成后再绑定")
        if session.budget_accounting == "gateway" and session.task_id and session.task_id != task_id:
            raise Conflict("受管预算身份不能直接改绑，请为新任务创建执行会话")
        task = self.task(task_id)
        host = self.store.get("host", session.host_id)
        if task.host_id and session.host_id != task.host_id and (host or {}).get("machine_id") != task.host_id:
            raise Conflict("会话不在指定主机上")
        if not within_scope(session.cwd, task.allowed_roots):
            raise Conflict("会话工作目录不在任务范围内")
        if task.cwd and PurePosixPath(task.cwd) != PurePosixPath(session.cwd):
            raise Conflict("会话工作目录与任务指定目录不一致")
        if task.allowed_models and session.model not in task.allowed_models:
            raise Conflict("会话模型不在任务允许列表中")
        session.task_id, session.control_enabled = task_id, control_enabled
        session.binding_source = "manual"
        session.control_revision += 1
        return self.save("session", session, expected=session.revision)

    def mark_progress(self, session_id: str, evidence: str) -> dict:
        session = self.session(session_id)
        session.last_progress_at = self.clock()
        session.progress_revision += 1
        session.diagnosis = None
        self.store.event("progress", session_id, {"evidence": evidence})
        result = self.save("session", session, expected=session.revision)
        self._finish_recovery_on_progress(session)
        return result

    def _finish_recovery_on_progress(self, session: Session) -> None:
        for recovery in self.store.list("recovery"):
            if recovery["session_id"] == session.id and recovery["state"] == "awaiting_progress" and session.progress_revision > recovery["expected_progress_revision"]:
                recovery["state"] = "complete"
                recovery["steps"].append({"state": "progress_verified", "at": self.clock()})
                self.save("recovery", recovery)
            elif recovery["session_id"] == session.id and recovery["state"] in {"blocked", "reconcile_required"} and session.progress_revision > recovery["expected_progress_revision"]:
                recovery.update(state="cancelled", error="原会话已有新的有效进展，结束过期恢复，不再重发压缩或接续")
                self.save("recovery", recovery)
        for job in self.store.list("decision"):
            if job["session_id"] == session.id and job["state"] == "pending" and job.get("progress_revision", -1) < session.progress_revision:
                job["state"] = "resolved"
                self.save("decision", job)

    def diagnose(self, session_id: str, cause: str, confirmed: bool, reason: str) -> dict:
        session = self.session(session_id)
        session.diagnosis = {"cause": cause, "confirmed": confirmed, "reason": reason,
                             "progress_revision": session.progress_revision, "at": self.clock()}
        return self.save("session", session, expected=session.revision)

    def snapshot(self) -> dict:
        with self.store.lock:
            return self._snapshot()

    def assess(self, session: Session, related: list[dict] | None = None) -> dict:
        decision = assess_stall(session, self.clock())
        if decision["state"] in {"suspected_stall", "confirmed_stall"}:
            for child in related if related is not None else self.store.list("session"):
                if child.get("parent_thread_id") == session.thread_id and child["host_id"] == session.host_id and child["connected"] and child["status"] == "active":
                    if max(child["last_progress_at"], child.get("last_output_at") or 0) > self.clock() - session.stall_policy.suspect_after:
                        return {"state": "waiting_children", "eligible": False, "reason": "子代理仍在推进，保持父会话后端"}
        return decision

    def _snapshot(self) -> dict:
        now = self.clock()
        sessions = self.store.list("session")
        tasks = self.store.list("task")
        children: dict[str, list[dict]] = {}
        for task in tasks:
            if task["parent_id"]:
                children.setdefault(task["parent_id"], []).append(task)
        for task in tasks:
            direct = children.get(task["id"], [])
            task["child_task_count"] = len(direct)
            task["completed_child_task_count"] = sum(child["status"] == "complete" and bool((child.get("verification") or {}).get("passed")) for child in direct)
        for value in sessions:
            session = Session.model_validate(value)
            value["stall"] = self.assess(session, sessions)
            value["child_count"] = sum(s.get("parent_thread_id") == session.thread_id and s["host_id"] == session.host_id for s in sessions)
            if session.goal:
                value["goal_elapsed_seconds"] = session.goal.get("timeUsedSeconds")
                value["goal_elapsed_source"] = "codex"
        return {"updated_at": now, "cursor": self.store.cursor(),
                "tasks": tasks, "sessions": sessions,
                "hosts": self.store.list("host"), "backends": self.store.list("backend"),
                "routes": self.store.list("route"), "recoveries": [public_recovery(record) for record in self.store.list("recovery")],
                "proposals": self.store.list("proposal"), "allocations": self.store.allocations(),
                "decisions": self.store.list("decision"),
                "defaults": {"task_budget": self.config.default_task_budget.model_dump(), "request_gateway": self.config.inference.enabled},
                "supervisor": {"configured": bool(self.config.supervisor)}}

    def report(self) -> str:
        state = self.snapshot()
        lines = [f"工作看板 · 更新时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state['updated_at']))}"]
        labels = {"planned": "待安排", "ready": "就绪", "running": "执行中", "active": "执行中", "idle": "空闲",
            "unloaded": "未加载", "notLoaded": "未加载", "unknown": "状态未核对", "systemError": "会话错误",
            "usageLimited": "用量受限", "budgetLimited": "预算受限", "compacting": "原生压缩中",
            "complete": "已完成", "paused": "已暂停", "blocked": "已阻塞", "failed": "失败", "waiting_resource": "等待资源",
            "observation_lost": "观测断开", "suspected_stall": "疑似停滞", "confirmed_stall": "确认停滞",
            "waiting": "等待中", "slow_streaming": "持续输出", "tool_running": "工具执行中", "observing": "观察中"}
        def phase(session):
            if session.get("budget_accounting") == "gateway" and not session.get("budget_ready"):
                return "预算入口待核对"
            if session.get("compaction_turn_id"):
                owned = any(record["session_id"] == session["id"] and record["state"] not in {"complete", "cancelled", "blocked"} for record in state["recoveries"])
                return "恢复压缩中" if owned else "原生压缩中"
            return labels.get(session["stall"]["state"], session["stall"]["state"])
        for task in sorted(state["tasks"], key=lambda t: (-t["priority"], t["created_at"])):
            attached = [s for s in state["sessions"] if s["task_id"] == task["id"]]
            suffix = "；".join(f"{s['host_id']} / {s['model']} / {phase(s)}" + (
                f" / goal 累计 {s['goal'].get('timeUsedSeconds', 0)} 秒" if s.get('goal') else "") for s in attached)
            lines.append(f"• {task['title']} [{labels.get(task['status'], task['status'])}] {suffix}")
            if task["child_task_count"]:
                lines.append(f"  子任务已验收 {task['completed_child_task_count']}/{task['child_task_count']}")
            lines.append(f"  用量：{task['requests_used']} 请求 / {task['tokens_used']:,} tokens；修复 {task['retries_used']} 次")
            if task.get("blocked_reason"):
                lines.append(f"  阻塞：{task['blocked_reason']}")
            if task.get("next_step"):
                lines.append(f"  下一步：{task['next_step']}")
            if task.get("verification"):
                lines.append("  验收：" + task["verification"]["evidence"][:280])
        if not state["tasks"]:
            lines.append("尚未录入工作任务。")
        for session in [s for s in state["sessions"] if not s["task_id"]]:
            lines.append(f"• 未归属会话 {session['thread_id'][:8]}：{session['host_id']} / {session['cwd']} / {session['model']} / {phase(session)}")
        decisions = [job for job in state["decisions"] if job["state"] in {"pending", "blocked", "waiting_user"}]
        if decisions:
            lines.append(f"待处理异常或验收：{len(decisions)} 项" + ("；未配置自动决策器" if not state["supervisor"]["configured"] else ""))
        pending = sum(p.get("state") == "pending" for p in state["proposals"])
        if pending:
            lines.append(f"需要你决定：{pending} 项范围或预算变更。")
        return "\n".join(lines)

    async def start(self) -> None:
        self._stopping = False
        # 旧进程的 HTTP 连接已不存在；释放并发，未知消耗保守结算且不退还请求数。
        for request in self.store.active_inference():
            self.store.finish_inference(request["id"], None, "interrupted_on_restart")
        # 未附着会话的预留发生在 turn/start 之前，旧进程不可能已用它发出工作。
        orphan_groups = {row["id"].split("/", 1)[0] for row in self.store.allocations() if row["session_id"] is None}
        for allocation_id in orphan_groups:
            self.store.release_group(allocation_id)
            self.store.release(allocation_id)
            self.store.event("allocation.released_on_restart", allocation_id, {"reason": "未附着执行会话的预留"})
        for record in self.store.list("recovery"):
            if record["state"] not in {"complete", "cancelled", "blocked", "awaiting_progress", "reconcile_required"}:
                record.update(state="reconcile_required", error="服务重启，须核对 daemon 中的压缩与接续结果；不会重发已有操作")
                self.save("recovery", record)
        for record in self.store.list("decision"):
            if record["state"] == "running":
                record.update(state="blocked", error="管理决策在服务退出时结果不确定，先核对原会话和操作记录，避免重复修复")
                self.save("decision", record)
        for value in self.store.list("session"):
            if value["connected"]:
                value["connected"] = False
                value["budget_ready"] = False
                self.save("session", value)
        for host in self.store.list("host"):
            if host["enabled"]:
                self.jobs.append(asyncio.create_task(self._host_loop(host["id"])))
        self.jobs.append(asyncio.create_task(self._metrics_loop()))
        self.jobs.append(asyncio.create_task(self._policy_loop()))
        if self.config.supervisor:
            self.jobs.append(asyncio.create_task(self._supervisor_loop()))

    async def stop(self) -> None:
        self._stopping = True
        for task in self.jobs:
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        self.jobs.clear()
        for task in self.background_controls.values():
            task.cancel()
        await asyncio.gather(*self.background_controls.values(), return_exceptions=True)
        self.background_controls.clear()
        await asyncio.gather(*(client.close() for client in list(self.clients.values())), return_exceptions=True)
        self.clients.clear()

    async def _metrics_loop(self) -> None:
        async with httpx.AsyncClient(timeout=8) as http:
            while True:
                for row in self.store.list("backend"):
                    backend = Backend.model_validate(row)
                    if not backend.metrics_url or backend.opaque:
                        continue
                    try:
                        headers = {}
                        if backend.metrics_token_env:
                            headers["Authorization"] = f"Bearer {os.environ[backend.metrics_token_env]}"
                        response = await http.get(backend.metrics_url, headers=headers)
                        response.raise_for_status()
                        measured = parse_metrics(response.text)
                        if not measured:
                            raise ValueError("响应中没有可识别的 vLLM 指标")
                        own = sum(backend.id in row["backend_ids"] for row in self.store.active_inference())
                        measured["external_running"] = max(0, measured.get("running", 0) - own)
                        interval = self.clock() - backend.observed_at if backend.observed_at is not None else 0
                        if interval > 0:
                            for counter, output in [("prompt_tokens_total", "prompt_tps"), ("generation_tokens_total", "generation_tps")]:
                                if counter in measured and counter in backend.metrics:
                                    measured[output] = max(0, measured[counter] - backend.metrics[counter]) / interval
                        if measured.get("ttft_count", 0) > 0:
                            measured["ttft_mean_seconds"] = measured.get("ttft_sum", 0) / measured["ttft_count"]
                        backend.metrics, backend.observed_at = measured, self.clock()
                        backend.health, backend.last_error = "available", None
                    except Exception as exc:
                        backend.health, backend.last_error = "unavailable", type(exc).__name__
                    self.save("backend", backend)
                await asyncio.sleep(self.config.poll_interval)

    async def _policy_loop(self) -> None:
        while True:
            for row in self.store.list("session"):
                session = Session.model_validate(row)
                decision = self.assess(session)
                if not session.control_enabled or not session.task_id:
                    continue
                if decision["state"] == "suspected_stall" or decision.get("needs_diagnosis"):
                    from .supervisor import queue_decision
                    queue_decision(self, session, decision["reason"], "diagnose", self.clock())
                if decision["eligible"] and session.id not in self.background_controls:
                    pending_recovery = any(r["session_id"] == session.id and r["state"] not in {"blocked", "complete", "cancelled"} for r in self.store.list("recovery"))
                    if pending_recovery:
                        continue
                    task = self.task(session.task_id)
                    target = self.select_route(task)
                    if target and target["model"] != session.model:
                        job = asyncio.create_task(self.recover(session.id, target["model"]))
                        self.background_controls[session.id] = job
                        def done(future, session_id=session.id):
                            self.background_controls.pop(session_id, None)
                            if not future.cancelled() and future.exception():
                                self.store.event("recovery.error", session_id, {"error": str(future.exception())})
                        job.add_done_callback(done)
            if self.config.auto_dispatch:
                tasks = sorted(self.store.list("task"), key=lambda t: (-t["priority"], t["created_at"]))
                for row in tasks:
                    if row["status"] in {"ready", "waiting_resource"}:
                        with contextlib.suppress(Conflict):
                            await self.dispatch(row["id"])
            await asyncio.sleep(2)

    async def _supervisor_loop(self) -> None:
        from .supervisor import Supervisor
        supervisor = Supervisor(self)
        while True:
            for job in self.store.list("decision"):
                if job["state"] == "pending" and job.get("not_before", 0) <= self.clock():
                    session = self.session(job["session_id"])
                    if session.progress_revision != job.get("progress_revision", session.progress_revision):
                        job["state"] = "resolved"
                        self.save("decision", job)
                        continue
                    await supervisor.process(job["id"])
            await asyncio.sleep(2)

    async def _host_loop(self, host_id: str) -> None:
        while not self._stopping:
            client = None
            consumer = None
            try:
                host = Host.model_validate(self.store.require("host", host_id))
                host.connection = "connecting"
                self.save("host", host)
                if not host.control_socket:
                    discovered = await probe_host(host)
                    host.architecture = discovered["architecture"]
                    host.capabilities["discovery"] = discovered
                    endpoints = discovered["endpoints"]
                    if not endpoints:
                        self.save("host", host)
                        raise ConnectionError("未发现该 SSH 账号可接入的 Codex 控制 socket；独立 stdio 会话需通过共享 daemon 启动后接入")
                    first = endpoints[0]
                    host.control_socket, host.codex_home = first["socket"], first["codex_home"]
                    host.auto_discovered_socket = True
                    self.save("host", host)
                    for endpoint in endpoints[1:]:
                        child_id = host_id + "@" + hashlib.sha256(endpoint["socket"].encode()).hexdigest()[:8]
                        if self.store.get("host", child_id):
                            continue
                        child = Host.model_validate({**host.model_dump(), "id": child_id, "revision": 0,
                            "name": host.name + " · " + endpoint["codex_home"], "machine_id": host_id,
                            "discovered": True, "control_socket": endpoint["socket"], "codex_home": endpoint["codex_home"]})
                        self.save("host", child, expected=0)
                        self.jobs.append(asyncio.create_task(self._host_loop(child_id)))
                if self.config.inference.enabled and host.gateway_remote_port:
                    host.gateway_local_port = self.config.port
                client = await CodexClient.ssh(host)
                self.clients[host_id] = client
                self.host_ready.setdefault(host_id, asyncio.Event()).set()
                host.connection = "connected"
                host.last_error = None
                self.save("host", host)
                consumer = asyncio.create_task(self._consume(host_id, client))
                subscribed: set[str] = set()
                first_sync = True
                while client.connected:
                    if consumer.done():
                        consumer.result()
                    await self.sync_host(host_id, client, subscribed)
                    for record in self.store.list("recovery"):
                        if (record["state"] == "reconcile_required" or first_sync and record["state"] == "awaiting_progress") and self.session(record["session_id"]).host_id == host_id:
                            await self.reconcile_recovery(record["id"])
                    first_sync = False
                    await asyncio.sleep(self.config.poll_interval)
                raise ConnectionError("控制连接已结束")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                host = self.store.require("host", host_id)
                host.update(connection="error", last_error=f"{type(exc).__name__}: {exc}")
                if host.get("auto_discovered_socket"):
                    host["control_socket"] = ""
                self.save("host", host)
            finally:
                if consumer:
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
                self.clients.pop(host_id, None)
                self.worker_verified.discard(host_id)
                self.host_ready.setdefault(host_id, asyncio.Event()).clear()
                if client:
                    await client.close()
                for value in self.store.list("session"):
                    if value["host_id"] == host_id and value["connected"]:
                        value["connected"] = False
                        value["budget_ready"] = False
                        self.save("session", value)
            await asyncio.sleep(self.config.reconnect_interval)

    async def sync_host(self, host_id: str, client: CodexClient, subscribed: set[str]) -> None:
        host_record = self.store.get("host", host_id)
        managed = [value for value in self.store.list("session") if value["host_id"] == host_id
            and value.get("budget_accounting") == "gateway"]
        if managed and self.config.inference.enabled and host_id not in self.worker_verified:
            try:
                await self.verify_worker(Host.model_validate(host_record), client)
            except Conflict as exc:
                current = self.store.require("host", host_id)
                current["capabilities"]["request_budget"] = {"verified": False, "reason": str(exc)}
                self.save("host", current)
        cursor = None
        loaded = set()
        while True:
            page = await client.call("thread/loaded/list", {"cursor": cursor, "limit": 100})
            for item in page.get("data", []):
                thread_id = item if isinstance(item, str) else item["id"]
                loaded.add(thread_id)
                query_started = self.clock()
                initial_subscription = thread_id not in subscribed
                if thread_id not in subscribed:
                    try:
                        result = await client.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                        subscribed.add(thread_id)
                    except RpcError as exc:
                        if not any(marker in str(exc) for marker in ["no rollout found", "list_turns is not supported yet"]):
                            raise
                        # 新建空会话的历史尚未就绪，不能因此断开整台主机。
                        session_id = f"{host_id}:{thread_id}"
                        value = self.store.get("session", session_id)
                        session = Session.model_validate(value) if value else Session(id=session_id, host_id=host_id, thread_id=thread_id)
                        session.connected, session.last_error = True, "会话历史初始化中，等待下次核对"
                        self.save("session", session)
                        continue
                else:
                    result = await client.call("thread/read", {"threadId": thread_id})
                thread = result["thread"]
                history_thread = None
                if initial_subscription:
                    history = await client.call("thread/read", {"threadId": thread_id, "includeTurns": True})
                    history_thread = history["thread"]
                    self.backfill_history(host_id, history_thread)
                goal_started = self.clock()
                goal_known, goal = False, None
                try:
                    goal = (await client.call("thread/goal/get", {"threadId": thread_id})).get("goal")
                    goal_known = True
                except RpcError:
                    pass
                # 先完成 RPC，再合并最新状态，不能用旧对象覆盖同时到达的进展或人工操作。
                session_id = f"{host_id}:{thread_id}"
                previous = self.store.get("session", session_id)
                session = Session.model_validate(previous) if previous else Session(id=session_id, host_id=host_id, thread_id=thread_id)
                if session.role != "supervisor":
                    session.name = thread.get("name") or thread.get("preview", "")[:120]
                if session.settings_observed_at <= query_started:
                    model = thread.get("model") or result.get("model", session.model)
                    cwd = thread.get("cwd", session.cwd)
                    changed = bool(session.model and model != session.model or session.cwd and cwd != session.cwd)
                    intent = self.store.get("model_intent", session_id)
                    ours = bool(intent and not intent["consumed"] and intent["model"] == model
                        and intent["control_revision"] == session.control_revision and cwd == session.cwd)
                    if changed and session.control_enabled and not ours:
                        session.control_revision += 1
                        session.diagnosis = None
                        session.last_external_model, session.last_external_model_at = model, self.clock()
                    if ours:
                        intent["consumed"] = True
                        self.save("model_intent", intent)
                    session.cwd, session.model = cwd, model
                    session.model_provider = thread.get("modelProvider") or result.get("modelProvider", session.model_provider)
                self._inherit_agent_task(session, thread)
                if session.status_observed_at <= query_started:
                    session.status = thread.get("status", {}).get("type", session.status)
                    if history_thread is not None:
                        active = [turn for turn in history_thread.get("turns", []) if turn.get("status") == "inProgress"]
                        session.active_turn_id = active[-1]["id"] if active else None
                        session.compaction_turn_id = next((turn["id"] for turn in active if any(item.get("type") == "contextCompaction" for item in turn.get("items", []))), None)
                        session.tool_running = any(item.get("type") == "commandExecution" and item.get("status") == "inProgress" for turn in active for item in turn.get("items", []))
                    flags = thread.get("status", {}).get("activeFlags", [])
                    if "waitingOnApproval" in flags:
                        session.waiting_reason = "等待权限确认"
                    elif "waitingOnUserInput" in flags:
                        session.waiting_reason = "等待用户输入"
                session.connected = True
                if session.budget_accounting == "gateway":
                    session.budget_ready = host_id in self.worker_verified
                if goal_known and session.goal_observed_at <= goal_started:
                    if previous and session.control_enabled and (session.goal or {}).get("objective") != (goal or {}).get("objective"):
                        session.control_revision += 1
                        session.diagnosis = None
                    session.goal, session.goal_observed_at = goal, self.clock()
                with contextlib.suppress(Conflict):
                    self.save("session", session, expected=session.revision)
            cursor = page.get("nextCursor")
            if not cursor:
                break
        for value in self.store.list("session"):
            if value["host_id"] == host_id and value["thread_id"] not in loaded:
                task = self.task(value["task_id"]) if value.get("task_id") else None
                if (value.get("budget_accounting") == "gateway" and value.get("binding_source") == "managed"
                    and value.get("control_enabled") and task and task.status == "running"
                    and host_id in self.worker_verified and within_scope(value["cwd"], task.allowed_roots)
                    and (not task.cwd or task.cwd == value["cwd"])):
                    # 沿用原 provider ID；不设置 model/cwd，保留用户最后一次原生选择。
                    # loaded 会话的 resume 忽略 config，此时由专用 worker 的持久身份兜底。
                    host = Host.model_validate(host_record)
                    try:
                        await client.call("thread/resume", {"threadId": value["thread_id"], "excludeTurns": True,
                            "modelProvider": value["model_provider"],
                            "config": self.managed_overrides(task, host, value["model_provider"])})
                        self.store.event("budget.worker_restored", value["id"], {"task_id": task.id})
                        continue
                    except RpcError as exc:
                        value["last_error"] = f"受管会话恢复失败：{exc}"
                value.update(status="unloaded", connected=True, active_turn_id=None)
                value["budget_ready"] = False
                self.save("session", value)

    def backfill_history(self, host_id: str, thread: dict) -> None:
        """重连补齐权威历史里的完成项；不把旧工具结果重新算成当前进展。"""
        session_id = f"{host_id}:{thread['id']}"
        recovered = 0
        with self.store.transaction():
            current = self.store.get("session", session_id)
            observed = Session.model_validate(current) if current and current.get("task_id") and current.get("control_enabled") else None
            recoveries = [record for record in self.store.list("recovery") if record["session_id"] == session_id]
            known_items = {item_id for record in recoveries for item_id in record.get("baseline_completed_item_ids") or []}
            legacy_known_turns = {turn_id for record in recoveries if record.get("baseline_completed_item_ids") is None for turn_id in record.get("baseline_turn_ids") or []}
            completed_work = False
            for turn in thread.get("turns", []):
                if turn.get("status") not in {"completed", "interrupted", "failed"}:
                    continue
                for item in turn.get("items", []):
                    if not item.get("id") or self.store.completed_item_seq(session_id, item["id"]) is not None:
                        continue
                    self.store.event("codex.event", session_id, {"method": "item/completed", "historyRecovered": True,
                        "params": {"threadId": thread["id"], "turnId": turn["id"], "item": item}})
                    recovered += 1
                    if observed and item["id"] not in known_items:
                        if item.get("type") == "userMessage" and turn["id"] not in legacy_known_turns and not self.store.is_manager_message(session_id, item.get("clientId")):
                            observed.control_revision += 1
                            observed.diagnosis = None
                        completed_at = item.get("completedAtMs")
                        at = completed_at / 1000 if completed_at is not None else turn.get("completedAt")
                        if at is not None:
                            at = min(float(at), self.clock())
                            if item.get("type") == "agentMessage" and item.get("text"):
                                observed.last_output_at = max(observed.last_output_at or 0, at)
                            success = (item.get("type") == "commandExecution" and item.get("exitCode") == 0
                                or item.get("type") == "fileChange" and item.get("status") == "completed")
                            if success and at > observed.last_progress_at:
                                observed.last_progress_at = at
                                observed.progress_revision += 1
                                observed.diagnosis = None
                        if item.get("type") in {"commandExecution", "fileChange", "agentMessage"} and turn.get("status") == "completed":
                            completed_work = True
            if observed:
                self.save("session", observed, expected=observed.revision)
                if completed_work and self.task(observed.task_id).acceptance:
                    from .supervisor import queue_decision
                    queue_decision(self, observed, "重连发现已完成回合，请核对实际验收证据", "verify", self.clock()+5)
            self.store.event("observation.history_reconciled", session_id, {"recovered_items": recovered,
                "coverage": "权威历史中保留的完成项；断线期间的逐字时间、瞬时错误和中间状态无法完整重建"})

    async def _consume(self, host_id: str, client: CodexClient) -> None:
        while True:
            try:
                event = await asyncio.wait_for(client.events.get(), 0.5)
                await asyncio.sleep(0.02)
                batch = [event]
                while not client.events.empty() and len(batch) < 256:
                    batch.append(client.events.get_nowait())
                with self.store.transaction():
                    for item in batch:
                        try:
                            self.ingest(host_id, item)
                        except (KeyError, TypeError, ValueError) as exc:
                            self.store.event("observation.invalid_event", host_id,
                                {"method": item.get("method"), "error": str(exc)})
            except TimeoutError:
                pass
            while not client.server_requests.empty():
                request = client.server_requests.get_nowait()
                # 需要权限决策的服务端请求保留为待处理，不默认为批准。
                self.store.event("approval.requested", host_id, request)
            if client.resync_required:
                self.store.event("observation.resync_required", host_id, {"reason": "订阅缓冲已满，需核对权威状态"})
                client.resync_required = False
                # 丢失的开始/结束事件会影响控制状态；重建订阅才能补齐权威历史。
                raise ConnectionError("事件缓冲溢出，重新连接并核对会话状态")

    def ingest(self, host_id: str, event: dict) -> None:
        params = event.get("params", {})
        thread_id = params.get("threadId") or params.get("thread", {}).get("id")
        if not thread_id:
            return
        session_id = f"{host_id}:{thread_id}"
        if event.get("method") == "item/completed" and (item_id := params.get("item", {}).get("id")) and self.store.completed_item_seq(session_id, item_id) is not None:
            return
        value = self.store.get("session", session_id)
        session = Session.model_validate(value) if value else Session(id=session_id, host_id=host_id, thread_id=thread_id)
        method = event["method"]
        now = self.clock()
        if method.endswith("/delta") and session.last_output_at and now - session.last_output_at < 1:
            self.store.event("codex.event", session_id, event)
            return
        session.connected, session.last_activity_at = True, now
        if method == "thread/started":
            self._inherit_agent_task(session, params.get("thread", {}))
        if method.endswith("/delta") and (params.get("delta") or params.get("outputDelta")):
            session.last_output_at = now
        if method == "turn/started":
            session.status_observed_at = now
            session.active_turn_id = params["turn"]["id"]
            session.status = "active"
        elif method == "turn/completed":
            session.status_observed_at = now
            turn = params.get("turn", {})
            interruption = (session_id, turn.get("id"))
            if turn.get("status") == "interrupted" and interruption not in self.expected_interrupts:
                session.control_revision += 1
                session.diagnosis = None
            self.expected_interrupts.discard(interruption)
            if session.active_turn_id == turn.get("id"):
                session.active_turn_id = None
            if session.compaction_turn_id == turn.get("id"):
                session.compaction_turn_id = None
            session.status = "idle"
            if turn.get("error"):
                session.last_error = str(turn["error"])
                for record in self.store.list("recovery"):
                    if record["session_id"] == session_id and record["state"] == "awaiting_progress":
                        record.update(state="blocked", error="接续回合失败：" + str(turn["error"]))
                        self.save("recovery", record)
            if session.task_id and self.task(session.task_id).status == "complete":
                self.store.release_task(session.task_id)
            elif session.task_id and session.control_enabled and not turn.get("error") and self.task(session.task_id).acceptance:
                from .supervisor import queue_decision
                queue_decision(self, session, "执行回合结束，请核对实际验收证据", "verify", now + 5)
        elif method == "thread/status/changed":
            session.status_observed_at = now
            session.status = params["status"]["type"]
            flags = params["status"].get("activeFlags", [])
            if "waitingOnApproval" in flags:
                session.waiting_reason = "等待权限确认"
            elif "waitingOnUserInput" in flags:
                session.waiting_reason = "等待用户输入"
            elif session.waiting_reason in {"等待权限确认", "等待用户输入"}:
                session.waiting_reason = None
        elif method == "thread/goal/updated":
            session.goal_observed_at = now
            if session.control_enabled and (session.goal or {}).get("objective") != (params.get("goal") or {}).get("objective"):
                session.control_revision += 1
                session.diagnosis = None
            session.goal = params["goal"]
        elif method == "thread/goal/cleared":
            session.goal_observed_at = now
            if session.control_enabled and session.goal:
                session.control_revision += 1
                session.diagnosis = None
            session.goal = None
        elif method == "thread/settings/updated":
            session.settings_observed_at = now
            settings = params["threadSettings"]
            expected = self.expected_models.get(session_id, [])
            expected[:] = [entry for entry in expected if now - entry[1] < 10]
            ours = bool(expected and expected[0][0] == settings["model"])
            if ours:
                expected.pop(0)
            intent = self.store.get("model_intent", session_id)
            if intent and not intent["consumed"] and intent["model"] == settings["model"] and intent["control_revision"] == session.control_revision:
                ours = True
                intent["consumed"] = True
                self.save("model_intent", intent)
            if (session.model and settings["model"] != session.model and not ours) or (session.cwd and settings["cwd"] != session.cwd):
                session.control_revision += 1
                session.diagnosis = None
                session.last_external_model, session.last_external_model_at = settings["model"], now
            session.model, session.cwd = settings["model"], settings["cwd"]
            session.model_provider = settings.get("modelProvider", session.model_provider)
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            total = usage.get("total", {}).get("totalTokens")
            if total is not None and total != session.last_token_total:
                if session.task_id and session.budget_accounting != "gateway":
                    delta = usage.get("last", {}).get("totalTokens", 0) if session.last_token_total is None else max(0, total - session.last_token_total)
                    if delta > 0:
                        self.store.charge_observed_usage(session.task_id, delta)
                session.last_token_total = total
        elif method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if method == "item/started" and item.get("type") == "contextCompaction":
                session.compaction_turn_id = params.get("turnId")
                owned = False
                for record in self.store.list("recovery"):
                    if record["session_id"] == session_id and record["state"] == "compressing":
                        owned = True
                        record["compaction_turn_id"] = params.get("turnId")
                        self.save("recovery", record)
                    elif record["session_id"] == session_id and record["state"] == "reconcile_required" and record.get("compaction_turn_id") == params.get("turnId"):
                        owned = True
                if not owned and session.control_enabled:
                    session.control_revision += 1
                    session.diagnosis = None
            if method == "item/completed" and item.get("type") == "contextCompaction":
                if session.compaction_turn_id == params.get("turnId"):
                    session.compaction_turn_id = None
                session.last_compaction_at = now
            if item.get("type") == "commandExecution":
                session.tool_running = method == "item/started"
            if item.get("type") == "collabAgentToolCall" and item.get("tool") in {"wait", "waitAgent", "wait_agent"}:
                session.waiting_reason = "等待子代理结果" if method == "item/started" else None
            if method == "item/started" and item.get("type") == "userMessage":
                message_id = item.get("clientId") or item.get("clientUserMessageId") or item.get("id")
                if message_id not in self.manager_message_ids and not self.store.is_manager_message(session_id, message_id):
                    session.control_revision += 1
                    session.diagnosis = None
            if method == "item/completed" and item.get("type") == "commandExecution" and item.get("exitCode") == 0:
                session.last_progress_at = now
                session.progress_revision += 1
                session.diagnosis = None
            elif method == "item/completed" and item.get("type") == "commandExecution" and item.get("exitCode") is not None:
                session.last_error = f"命令退出码 {item['exitCode']}：{item.get('command', '')}\n{item.get('aggregatedOutput', '')[-3000:]}"
                from .supervisor import queue_decision
                queue_decision(self, session, session.last_error, "tool_error", now + 15)
            if method == "item/completed" and item.get("type") == "fileChange" and item.get("status") == "completed":
                session.last_progress_at = now
                session.progress_revision += 1
                session.diagnosis = None
        elif method == "error":
            session.last_error = str(params.get("error", params))
            if not params.get("willRetry"):
                from .supervisor import queue_decision
                queue_decision(self, session, session.last_error, "error", now + 15)
        self.store.event("codex.event", session_id, event)
        self.save("session", session)
        if value and session.progress_revision > value["progress_revision"]:
            self._finish_recovery_on_progress(session)

    async def command(self, session_id: str, action: str, payload: dict,
                      operation_id: str, *, actor: str = "manager",
                      expected_control_revision: int | None = None) -> dict:
        if action not in {"message", "interrupt"}:
            raise Conflict("不支持的控制操作")
        if action == "message" and not payload.get("text", "").strip():
            raise Conflict("指令不能为空")
        self.session(session_id)
        operation = self.store.operation(operation_id, session_id, action, payload)
        if operation["state"] != "new":
            return {"operation_id": operation_id, "state": operation["state"], "result": operation["body"]}
        if actor == "user":
            # 在等待自动恢复锁前登记人工介入，使长压缩等待能及时让路。
            current = self.session(session_id)
            current.control_revision += 1
            current.diagnosis = None
            self.save("session", current)
        async with self.session_locks.setdefault(session_id, asyncio.Lock()):
            session = self.session(session_id)
            def reject(reason: str):
                self.store.finish_operation(operation_id, "not_sent", {"error": reason})
                raise Conflict(reason)
            if action == "message" and session.budget_accounting == "gateway" and not session.budget_ready:
                reject("受管推理入口尚未核对，暂不下发工作")
            if actor != "user":
                if not session.control_enabled or not session.task_id:
                    reject("会话尚未授权自动控制或未绑定工作任务")
                if expected_control_revision is not None and expected_control_revision != session.control_revision:
                    reject("控制状态已变化，取消过期的自动指令")
                task = self.task(session.task_id)
                if action == "message":
                    if session.compaction_turn_id and session.compaction_turn_id == session.active_turn_id:
                        reject("当前压缩回合尚未结束，暂不自动追加工作")
                    if task.status in {"complete", "paused", "failed", "blocked"}:
                        reject("任务当前状态不允许继续自动下发工作")
                    if task.allowed_models and session.model not in task.allowed_models:
                        reject("当前模型不在任务允许范围，不能自动下发工作")
                    if any(route["name"] == session.model and (not route["enabled"] or route["purpose"] == "recovery") for route in self.store.list("route")):
                        reject("当前模型入口已禁用或仅用于压缩，不能自动下发普通工作")
                    if not within_scope(session.cwd, task.allowed_roots):
                        reject("会话已越出任务目录范围")
                    if reason := budget_reason(task, self.clock()):
                        reject(reason)
                    if any(self.task(dep).status != "complete" for dep in task.dependencies):
                        reject("前置任务尚未完成，暂不下发新工作")
            client = self.clients.get(session.host_id)
            if not client or not client.connected:
                self.store.finish_operation(operation_id, "not_sent", {"error": "主机控制连接不可用"})
                raise Conflict("主机控制连接不可用")
            self.store.finish_operation(operation_id, "sent", {"action": action})
            try:
                if action == "message":
                    text = payload.get("text", "").strip()
                    if not text:
                        raise Conflict("指令不能为空")
                    params = self.message_params(session.thread_id, text, operation_id)
                    if session.active_turn_id:
                        method = "turn/steer"
                        params["expectedTurnId"] = session.active_turn_id
                    else:
                        method = "turn/start"
                    result = await client.call(method, params)
                elif action == "interrupt":
                    turn_id = payload.get("turn_id")
                    if not turn_id or turn_id != session.active_turn_id:
                        raise Conflict("运行回合已变化，拒绝中断其他回合")
                    result = await self.interrupt_turn(session_id, turn_id, client)
                else:
                    raise Conflict("不支持的控制操作")
                self.store.finish_operation(operation_id, "completed", {"result": result})
                return {"operation_id": operation_id, "state": "completed", "result": result}
            except (ConnectionError, TimeoutError) as exc:
                self.store.finish_operation(operation_id, "uncertain", {"error": str(exc), "reconcile_required": True})
                raise
            except Exception as exc:
                self.store.finish_operation(operation_id, "failed", {"error": str(exc)})
                raise

    def message_params(self, thread_id: str, text: str, operation_id: str) -> dict:
        message_id = f"sb-{operation_id}"
        self.manager_message_ids.add(message_id)
        if len(self.manager_message_ids) > 4096:
            self.manager_message_ids = {message_id}
        return {"threadId": thread_id, "input": [{"type": "text", "text": text}],
                "clientUserMessageId": message_id}

    async def change_model(self, session_id: str, model: str, client=None) -> dict:
        session = self.session(session_id)
        client = client or self.clients[session.host_id]
        self.expected_models.setdefault(session_id, []).append((model, self.clock()))
        self.save("model_intent", {"id": session_id, "model": model, "consumed": False,
            "control_revision": session.control_revision, "at": self.clock()})
        return await client.call("thread/settings/update", {"threadId": session.thread_id, "model": model})

    async def interrupt_turn(self, session_id: str, turn_id: str, client=None) -> dict:
        session = self.session(session_id)
        client = client or self.clients[session.host_id]
        self.expected_interrupts.add((session_id, turn_id))
        return await client.call("turn/interrupt", {"threadId": session.thread_id, "turnId": turn_id})

    async def wait_codex_event(self, session_id: str, after: int, predicate,
                              timeout: float = 120, expected_control_revision: int | None = None) -> dict:
        async with asyncio.timeout(timeout):
            cursor = after
            while True:
                current = self.session(session_id)
                if expected_control_revision is not None and current.control_revision != expected_control_revision:
                    raise Conflict("出现外部操作，取消自动恢复等待")
                for event in self.store.events(cursor, 1000, session_id):
                    cursor = event["seq"]
                    if event["kind"] == "codex.event" and predicate(event["body"]):
                        return event["body"]
                session = self.session(session_id)
                if not session.connected:
                    raise ConnectionError("等待事件时观测连接断开，需要核对后继续")
                await asyncio.sleep(0.05)

    async def recover(self, session_id: str, target_model: str) -> dict:
        async with self.session_locks.setdefault(session_id, asyncio.Lock()):
            session = self.session(session_id)
            decision = self.assess(session)
            if not decision["eligible"]:
                raise Conflict(f"不满足停滞恢复条件：{decision['reason']}")
            if not session.control_enabled or not session.task_id:
                raise Conflict("没有自动恢复授权或任务归属")
            task = self.task(session.task_id)
            if task.status in {"complete", "paused", "failed"}:
                raise Conflict("任务已完成或暂停，不进行自动恢复")
            previous = [r for r in self.store.list("recovery") if r["session_id"] == session_id and r["state"] not in {"complete", "cancelled"}]
            if previous:
                raise Conflict("存在尚未核对完成的恢复记录，不能重复压缩")
            if reason := budget_reason(task, self.clock()):
                raise Conflict(reason)
            if task.retries_used >= task.budget.max_retries:
                raise Conflict("恢复重试上限已到")
            if not within_scope(session.cwd, task.allowed_roots):
                raise Conflict("恢复目录不在原任务范围")
            if task.allowed_models and target_model not in task.allowed_models:
                raise Conflict("恢复目标模型超出任务范围")
            route = self.route(target_model)
            compression = self.route("ZL-DeepSeek-v4.1")
            if not route.enabled or route.purpose != "execution" or not compression.enabled or compression.purpose != "recovery":
                raise Conflict("恢复路由配置无效")
            if route.can_pin_backend and len(route.backend_ids) > 1:
                raise Conflict("恢复目标需要固定后端，但没有可用的固定方式")
            target_backends = [Backend.model_validate(self.store.require("backend", key)) for key in route.backend_ids]
            if any(not self.backend_available(backend) for backend in target_backends):
                raise Conflict("恢复目标后端不可用或指标已过期")
            recovery = Recovery(session_id=session_id, task_id=task.id, original_model=session.model,
                                target_model=target_model, expected_progress_revision=session.progress_revision,
                                expected_control_revision=session.control_revision, created_at=self.clock(),
                                scope_fingerprint=task_scope_fingerprint(task),
                                goal_objective=(session.goal or {}).get("objective"))
            if target_backends:
                recovery.allocation_id = f"recovery-{recovery.id}"
                if not self.store.reserve_recovery(recovery.allocation_id, task.id, session_id,
                    {backend.id: backend.max_sessions for backend in target_backends}, task.budget.max_sessions):
                    raise Conflict("恢复目标容量已经被其他会话占用")
            task.retries_used += 1
            self.save("task", task, expected=task.revision)
            self.save("recovery", recovery, expected=0)
            client = self.clients.get(session.host_id)
            if not client or not client.connected:
                recovery.state, recovery.error = "blocked", "控制连接不可用"
                if recovery.allocation_id:
                    self.store.release_group(recovery.allocation_id)
                return self.save("recovery", recovery)
            continuation_sent = False
            async def step(state: str):
                recovery.state = state
                recovery.steps.append({"state": state, "at": self.clock()})
                self.save("recovery", recovery)
                current = self.session(session_id)
                if current.progress_revision != recovery.expected_progress_revision or current.control_revision != recovery.expected_control_revision:
                    raise Conflict("原任务已恢复推进或出现人工操作，取消过期恢复")
                if current.last_output_at and current.last_output_at > recovery.created_at and state != "continuing":
                    raise Conflict("原会话重新开始输出，保持原执行后端")
            try:
                snapshot = await client.call("thread/read", {"threadId": session.thread_id, "includeTurns": True})
                recovery.baseline_turn_ids = [turn["id"] for turn in snapshot["thread"].get("turns", [])]
                recovery.baseline_completed_item_ids = [item["id"] for turn in snapshot["thread"].get("turns", [])
                    for item in turn.get("items", []) if item.get("id") and (item.get("completedAtMs") is not None
                    or turn.get("status") in {"completed", "interrupted", "failed"})]
                goal_read_at = self.clock()
                goal = (await client.call("thread/goal/get", {"threadId": session.thread_id})).get("goal")
                recovery.goal_objective = (goal or {}).get("objective")
                current = self.session(session_id)
                if current.goal_observed_at <= goal_read_at:
                    current.goal, current.goal_observed_at = goal, self.clock()
                    self.save("session", current)
                self.save("recovery", recovery)
                await step("preparing")
                if session.active_turn_id:
                    cursor = self.store.cursor()
                    await self.interrupt_turn(session_id, session.active_turn_id, client)
                    await self.wait_codex_event(session_id, cursor, lambda e: e["method"] == "turn/completed", expected_control_revision=recovery.expected_control_revision)
                await step("selecting_compression")
                await self.change_model(session_id, compression.name, client)
                await step("compressing")
                cursor = self.store.cursor()
                await client.call("thread/compact/start", {"threadId": session.thread_id})
                compact_event = await self.wait_codex_event(session_id, cursor, lambda e: (
                    e["method"] == "item/completed" and e.get("params", {}).get("item", {}).get("type") == "contextCompaction"
                    or e["method"] == "turn/completed" and e.get("params", {}).get("turn", {}).get("status") != "completed"), expected_control_revision=recovery.expected_control_revision)
                if compact_event["method"] == "turn/completed":
                    raise Conflict("压缩回合失败：" + str(compact_event["params"]["turn"].get("error") or compact_event["params"]["turn"].get("status")))
                compact_turn = compact_event["params"]["turnId"]
                recovery.compaction_turn_id = compact_turn
                finished = await self.wait_codex_event(session_id, cursor, lambda e: e["method"] == "turn/completed" and e["params"]["turn"]["id"] == compact_turn, expected_control_revision=recovery.expected_control_revision)
                if finished["params"]["turn"].get("status") != "completed":
                    raise Conflict("压缩回合未正常完成，不能继续迁移")
                recovery.steps.append({"state": "compression_completed", "turn_id": compact_turn, "at": self.clock()})
                self.save("recovery", recovery)
                await step("selecting_target")
                await self.change_model(session_id, target_model, client)
                await step("continuing")
                continuation_sent = True
                await self.continue_recovery(recovery, client)
            except asyncio.CancelledError:
                recovery.compaction_turn_id = recovery.compaction_turn_id or self.store.require("recovery", recovery.id).get("compaction_turn_id")
                recovery.state, recovery.error = "reconcile_required", "管理服务在恢复过程中停止，需要核对实际步骤"
                self.save("recovery", recovery)
                raise
            except Exception as exc:
                recovery.state, recovery.error = "blocked", str(exc)
                if isinstance(exc, ConnectionError | TimeoutError):
                    recovery.state = "reconcile_required"
                    recovery.compaction_turn_id = recovery.compaction_turn_id or self.store.require("recovery", recovery.id).get("compaction_turn_id")
                    if continuation_sent and self.store.get_operation(f"recovery-{recovery.id}-continue"):
                        self.store.finish_operation(f"recovery-{recovery.id}-continue", "uncertain", {"error": str(exc), "reconcile_required": True})
                    return self.save("recovery", recovery)
                if recovery.allocation_id:
                    self.store.release_group(recovery.allocation_id)
                # 设置恢复是幂等操作；不会重发压缩或工具命令。
                with contextlib.suppress(Exception):
                    if client.connected:
                        current = self.session(session_id)
                        if current.control_revision != recovery.expected_control_revision:
                            recovery.state = "cancelled"
                            record = self.store.require("recovery", recovery.id)
                            compact_id = record.get("compaction_turn_id")
                            if compact_id and current.active_turn_id == compact_id:
                                cursor = self.store.cursor()
                                await self.interrupt_turn(session_id, compact_id, client)
                                await self.wait_codex_event(session_id, cursor, lambda e:e["method"] == "turn/completed", timeout=10)
                            restore = current.last_external_model if current.last_external_model_at and current.last_external_model_at >= recovery.created_at else recovery.original_model
                            await self.change_model(session_id, restore, client)
                        else:
                            await self.change_model(session_id, recovery.original_model, client)
            return self.save("recovery", recovery)

    def validate_recovery_continuation(self, recovery: Recovery) -> tuple[Session, Task]:
        session = self.session(recovery.session_id)
        task = self.task(recovery.task_id)
        if session.control_revision != recovery.expected_control_revision or session.task_id != task.id or not session.control_enabled:
            raise Conflict("控制权或任务归属已变化，取消恢复接续")
        if task_scope_fingerprint(task) != recovery.scope_fingerprint or not within_scope(session.cwd, task.allowed_roots):
            raise Conflict("任务范围已变化，取消旧恢复接续")
        if task.status in {"complete", "failed", "paused", "blocked"}:
            raise Conflict("任务当前状态不允许恢复接续")
        if reason := budget_reason(task, self.clock()):
            raise Conflict(reason)
        if (session.goal or {}).get("objective") != recovery.goal_objective:
            raise Conflict("原生 goal 目标已变化，取消旧恢复接续")
        return session, task

    async def continue_recovery(self, recovery: Recovery, client: CodexClient) -> None:
        """调用方持有会话锁；仅发送一次已证明可以接续的工作。"""
        session, task = self.validate_recovery_continuation(recovery)
        continuation_id = f"recovery-{recovery.id}-continue"
        previous = self.store.get_operation(continuation_id)
        text = previous["body"].get("request", {}).get("text") if previous else None
        text = text or ("继续原工作任务，先确认当前进度，再推进下一步。以下为任务系统保存的原始要求，不能因上下文压缩而改变：\n"
            + json.dumps({"title": task.title, "objective": task.objective, "cwd": task.cwd,
                "allowed_roots": task.allowed_roots, "acceptance": task.acceptance,
                "allowed_models": task.allowed_models, "budget": task.budget.model_dump(),
                "usage": {"requests": task.requests_used, "tokens": task.tokens_used, "retries": task.retries_used}}, ensure_ascii=False)
            + "\n在既定范围和剩余预算内完成并实际验证；改变目标或扩大范围时先报告。")
        operation = self.store.operation(continuation_id, session.id, "message", {"text": text})
        if operation["state"] not in {"new", "planned"}:
            raise Conflict("接续指令已有发送记录，需要核对实际会话后继续")
        self.store.finish_operation(continuation_id, "sent", {"recovery_id": recovery.id})
        result = await client.call("turn/start", self.message_params(session.thread_id, text, continuation_id))
        self.store.finish_operation(continuation_id, "completed", {"result": result})
        recovery.continuation_turn_id = result.get("turn", {}).get("id")
        if recovery.allocation_id:
            self.store.commit_recovery_allocation(session.id, recovery.allocation_id)
        route = self.route(recovery.target_model)
        session = self.session(session.id)
        session.backend_id = route.backend_ids[0] if len(route.backend_ids) == 1 else None
        session.last_recovery_at = self.clock()
        self.save("session", session)
        recovery.state, recovery.error = "awaiting_progress", None

    async def reconcile_recovery(self, recovery_id: str) -> dict:
        from .reconciliation import reconcile_recovery
        return await reconcile_recovery(self, recovery_id)
