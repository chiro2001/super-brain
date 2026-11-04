from __future__ import annotations

import hashlib
import json
import time
from pathlib import PurePosixPath

from .models import Session, Task


def task_scope_fingerprint(task: Task) -> str:
    scope = task.model_dump(include={"objective", "host_id", "cwd", "allowed_roots", "allowed_models", "acceptance"})
    return hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def within_scope(path: str, roots: list[str]) -> bool:
    """被执行目录为 Linux 路径；远端符号链接仍须在实际执行前核验。"""
    value = PurePosixPath(path)
    if not value.is_absolute() or ".." in value.parts:
        return False
    return any(value.is_relative_to(PurePosixPath(root)) for root in roots if root.startswith("/") and ".." not in PurePosixPath(root).parts)


def budget_reason(task: Task, now: float | None = None) -> str | None:
    budget = task.budget
    if budget.max_requests is None and budget.max_tokens is None and budget.max_seconds is None:
        return "尚未配置推理预算"
    if budget.max_requests is not None and task.requests_used >= budget.max_requests:
        return "请求预算耗尽"
    if budget.max_tokens is not None and task.tokens_used >= budget.max_tokens:
        return "token 预算耗尽"
    if task.retries_used > budget.max_retries:
        return "重试上限耗尽"
    if budget.max_seconds is not None and task.started_at is not None and (time.time() if now is None else now) - task.started_at >= budget.max_seconds:
        return "任务执行窗口预算耗尽"
    return None


def assess_stall(session: Session, now: float) -> dict:
    if not session.connected:
        return {"state": "observation_lost", "eligible": False, "reason": "观测连接已断开"}
    if session.compaction_turn_id and session.compaction_turn_id == session.active_turn_id:
        return {"state": "compacting", "eligible": False, "reason": "已有原生压缩回合在运行，不能重复发起压缩"}
    if session.status not in {"active", "inProgress", "running"}:
        return {"state": session.status, "eligible": False, "reason": "当前没有已确认的活动工作"}
    if session.waiting_reason:
        return {"state": "waiting", "eligible": False, "reason": session.waiting_reason}
    elapsed = max(0, now - max(session.last_progress_at, session.last_compaction_at or 0))
    if session.last_output_at is not None and now - session.last_output_at < 60:
        return {"state": "slow_streaming", "eligible": False, "reason": "模型仍持续输出，保持当前后端",
                "needs_diagnosis": session.stall_policy.enabled and elapsed >= session.stall_policy.confirm_after,
                "elapsed": elapsed}
    if session.tool_running and (session.expected_tool_until is None or now < session.expected_tool_until):
        return {"state": "tool_running", "eligible": False, "reason": "工具仍在允许的执行阶段"}
    policy = session.stall_policy
    if not policy.enabled:
        return {"state": "observing", "eligible": False, "reason": "主动恢复尚未启用"}
    if elapsed < policy.suspect_after:
        return {"state": "running", "eligible": False, "reason": "仍在推进或正常等待", "elapsed": elapsed}
    if session.last_recovery_at and now - session.last_recovery_at < policy.cooldown:
        return {"state": "cooldown", "eligible": False, "reason": "恢复后冷却期", "elapsed": elapsed}
    diagnosis = session.diagnosis or {}
    confirmed = (elapsed >= policy.confirm_after and diagnosis.get("confirmed") is True
                 and diagnosis.get("cause") == "model_backend"
                 and diagnosis.get("progress_revision") == session.progress_revision
                 and diagnosis.get("at", 0) >= session.last_progress_at)
    return {"state": "confirmed_stall" if confirmed else "suspected_stall", "eligible": confirmed,
            "reason": diagnosis.get("reason", "需要进一步诊断，不能仅凭耗时迁移"), "elapsed": elapsed}
