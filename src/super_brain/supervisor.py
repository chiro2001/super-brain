from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import Session
from .policy import task_scope_fingerprint
from .store import Conflict


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["no_action", "diagnose", "repair", "verify", "ask_user"]
    reason: str
    message: str = ""
    cause: str = "unknown"
    confirmed: bool = False
    passed: bool = False
    evidence: str = ""
    evidence_event_ids: list[int] = Field(default_factory=list)


class Supervisor:
    def __init__(self, manager):
        self.manager = manager

    async def decide(self, job: dict) -> Decision:
        manager = self.manager
        config = manager.config.supervisor or {}
        session = manager.session(job["session_id"])
        task = manager.task(session.task_id)
        events = manager.store.decision_events(session.id, 40)
        context = {"task": task.model_dump(), "session": session.model_dump(),
                   "reason": job["reason"], "events": events}
        text = json.dumps(context, ensure_ascii=False)
        if len(text) > 60000:
            for event in events:
                item = event["body"].get("params", {}).get("item", {})
                for key in ("aggregatedOutput", "text"):
                    if isinstance(item.get(key), str) and len(item[key]) > 2000:
                        item[key] = item[key][-2000:]
        if config.get("kind") == "webhook":
            headers = {}
            if token_env := config.get("token_env"):
                import os
                headers["Authorization"] = f"Bearer {os.environ[token_env]}"
            async with httpx.AsyncClient(timeout=120) as http:
                response = await http.post(config["url"], json=context, headers=headers)
                response.raise_for_status()
                return Decision.model_validate(response.json())
        if config.get("kind") != "codex":
            raise Conflict("未配置可运行的管理决策驱动器")
        host_id = config["host_id"]
        client = manager.clients.get(host_id)
        if not client or not client.connected:
            raise Conflict("管理 agent 的 Codex 主机尚未连接")
        state = manager.store.get("supervisor", "codex") or {"id": "codex", "invocations": 0}
        if state["invocations"] >= config.get("max_invocations", 0):
            raise Conflict("管理决策调用预算已耗尽或尚未配置")
        thread_id = state.get("thread_id")
        if not thread_id:
            host = manager.store.require("host", host_id)
            cwd = config.get("cwd") or next(iter(host["allowed_roots"]), None)
            if not cwd:
                raise Conflict("管理 agent 需要明确工作目录")
            result = await client.call("thread/start", {"cwd": cwd, "model": config["model"],
                "modelProvider": config.get("model_provider"), "approvalPolicy": "never", "sandbox": "read-only",
                "config": {"agents.enabled": False},
                "developerInstructions": "你是 Super Brain 的管理决策器。只分析给定任务与事件并输出 JSON，不运行工具。只在原任务、目录和预算内建议修复。正常缓慢或持续输出时不迁移。任务变更交给用户。验证完成需要实际工具输出证据，不能仅依据助手自述。"})
            thread_id = result["thread"]["id"]
            state.update(thread_id=thread_id, host_id=host_id)
            manager.save("session", Session(id=f"{host_id}:{thread_id}", host_id=host_id, thread_id=thread_id,
                name="管理 agent", cwd=cwd, model=config["model"], connected=True, role="supervisor"))
        state["invocations"] += 1
        manager.save("supervisor", state)
        session_id = f"{host_id}:{thread_id}"
        cursor = manager.store.cursor()
        params = {"threadId": thread_id,
            "input": [{"type": "text", "text": "请判断以下事件的下一步，只输出符合 schema 的 JSON。repair 的 message 必须是原任务内的具体修复指令；verify 必须引用实际成功命令的事件 seq。\nSchema：" + json.dumps(Decision.model_json_schema(), ensure_ascii=False) + "\n任务与事件：" + json.dumps(context, ensure_ascii=False)}]}
        if config.get("structured_output", False):
            params["outputSchema"] = Decision.model_json_schema()
        await client.call("turn/start", params)
        completed = await manager.wait_codex_event(session_id, cursor, lambda e: e["method"] == "turn/completed", timeout=180)
        turn = completed.get("params", {}).get("turn", {})
        if turn.get("error") or turn.get("status") != "completed":
            raise Conflict("管理决策回合未成功完成：" + json.dumps(turn.get("error") or turn.get("status"), ensure_ascii=False))
        records = [event for event in manager.store.decision_events(session_id, 10) if event["seq"] > cursor]
        messages = [e["body"]["params"].get("item", {}).get("text", "") for e in records if e["kind"] == "codex.event" and e["body"]["method"] == "item/completed" and e["body"]["params"].get("item", {}).get("type") == "agentMessage"]
        output = messages[-1] if messages else "".join(e["body"].get("params", {}).get("delta", "") for e in records if e["kind"] == "codex.event" and e["body"]["method"] == "item/agentMessage/delta")
        output = output.strip()
        if output.startswith("```"):
            output = output.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return Decision.model_validate_json(output)

    async def process(self, job_id: str) -> dict:
        manager = self.manager
        job = manager.store.require("decision", job_id)
        if job["state"] != "pending":
            return job
        session = manager.session(job["session_id"])
        task = manager.task(session.task_id)
        if (task.status in {"complete", "paused", "failed"} or session.control_revision != job.get("control_revision", session.control_revision)
            or task.retries_used != job.get("retry_revision", task.retries_used)):
            job["state"] = "resolved"
            return manager.save("decision", job)
        progress_revision, control_revision = session.progress_revision, session.control_revision
        scope, retry_revision = task_scope_fingerprint(task), task.retries_used
        job.update(state="running", started_at=manager.clock())
        manager.save("decision", job)
        try:
            decision = await self.decide(job)
            current = manager.session(session.id)
            current_task = manager.task(task.id)
            if (current.progress_revision != progress_revision or current.control_revision != control_revision
                or current_task.status in {"complete", "paused", "failed"} or task_scope_fingerprint(current_task) != scope
                or current_task.retries_used != retry_revision):
                job.update(state="resolved", result="出现新进展或人工操作，丢弃过期决策")
                return manager.save("decision", job)
            job["decision"] = decision.model_dump()
            if decision.action == "diagnose":
                manager.diagnose(session.id, decision.cause, decision.confirmed, decision.reason)
            elif decision.action == "repair":
                task = manager.task(task.id)
                if task.retries_used >= task.budget.max_retries:
                    raise Conflict("任务修复重试上限已到")
                if not decision.message.strip():
                    raise Conflict("修复决策缺少具体指令")
                if task.status == "blocked" and (task.verification or {}).get("passed") is False:
                    task.status, task.blocked_reason = "running", None
                task.retries_used += 1
                manager.save("task", task, expected=task.revision)
                await manager.command(session.id, "message", {"text": decision.message}, f"repair-{job_id}", expected_control_revision=control_revision)
            elif decision.action == "verify":
                manager.verify_task(task.id, decision.passed, decision.evidence,
                                    event_ids=decision.evidence_event_ids, actor="manager")
            elif decision.action == "ask_user":
                task = manager.task(task.id)
                task.status, task.blocked_reason = "blocked", decision.reason
                manager.save("task", task, expected=task.revision)
                job["state"] = "waiting_user"
            job.setdefault("result", decision.reason)
            if job["state"] == "running":
                job["state"] = "complete"
        except Exception as exc:
            job.update(state="blocked", error=str(exc))
        return manager.save("decision", job)


def queue_decision(manager, session: Session, reason: str, source: str, not_before: float) -> None:
    if not session.task_id or not session.control_enabled:
        return
    task = manager.task(session.task_id)
    job_id = f"{source}:{session.id}:{session.progress_revision}:{session.control_revision}:{task.retries_used}"
    if manager.store.get("decision", job_id):
        return
    manager.save("decision", {"id": job_id, "session_id": session.id, "task_id": session.task_id,
        "state": "pending", "reason": reason, "source": source, "not_before": not_before,
        "created_at": manager.clock(), "progress_revision": session.progress_revision, "control_revision": session.control_revision,
        "retry_revision": task.retries_used})
