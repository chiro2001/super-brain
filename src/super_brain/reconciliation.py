"""恢复事务与 daemon 权威历史核对。任何不确定的已发送操作都不能重发。"""

from __future__ import annotations

import asyncio

from .models import Backend, Recovery
from .policy import budget_reason, task_scope_fingerprint
from .rpc import RpcError
from .store import Conflict


async def reconcile_recovery(manager, recovery_id: str) -> dict:
    record = manager.store.require("recovery", recovery_id)
    session_id = record["session_id"]
    async with manager.session_locks.setdefault(session_id, asyncio.Lock()):
        record = manager.store.require("recovery", recovery_id)
        if record["state"] not in {"reconcile_required", "awaiting_progress", "blocked"}:
            return record
        recovery = Recovery.model_validate(record)
        session = manager.session(session_id)
        client = manager.clients.get(session.host_id)
        if not client or not client.connected:
            raise Conflict("核对恢复事务需要可用的主机控制连接")

        def save(state: str, reason: str | None = None):
            recovery.state, recovery.error = state, reason
            result = manager.save("recovery", recovery)
            if state in {"cancelled", "blocked"} and recovery.allocation_id:
                operation = manager.store.get_operation(f"recovery-{recovery.id}-continue")
                if not operation or operation["state"] in {"planned", "not_sent"}:
                    manager.store.release_group(recovery.allocation_id)
            return result

        try:
            snapshot = await client.call("thread/read", {"threadId": session.thread_id, "includeTurns": True})
            thread = snapshot["thread"]
            manager.backfill_history(session.host_id, thread)
            session = manager.session(session_id)
            task = manager.task(recovery.task_id)
            if session.control_revision != recovery.expected_control_revision or session.task_id != task.id or not session.control_enabled:
                return save("cancelled", "人工控制或任务归属已变化，取消旧恢复")
            if not recovery.scope_fingerprint or recovery.baseline_turn_ids is None:
                return save("reconcile_required", "旧恢复记录缺少范围或回合基线，无法证明可以自动接续")
            if task_scope_fingerprint(task) != recovery.scope_fingerprint:
                return save("cancelled", "工作任务范围已变化，取消旧恢复")
            goal = (await client.call("thread/goal/get", {"threadId": session.thread_id})).get("goal")
            if (goal or {}).get("objective") != recovery.goal_objective:
                return save("cancelled", "原生 goal 目标已变化，取消旧恢复")
            turns = thread.get("turns", [])
            new_turns = [turn for turn in turns if turn["id"] not in recovery.baseline_turn_ids]
            continuation_id = f"recovery-{recovery.id}-continue"
            message_id = "sb-" + continuation_id
            continuation = None
            for turn in new_turns:
                for item in turn.get("items", []):
                    if item.get("type") != "userMessage":
                        continue
                    if item.get("clientId") == message_id:
                        continuation = turn
                    else:
                        return save("cancelled", "断线期间出现了其他输入，保持人工工作，不接续旧恢复")

            operation = manager.store.get_operation(continuation_id)
            if continuation:
                # 消息 ID 是 daemon 已接受工作请求的证据，与 RPC 回复是否丢失无关。
                if not operation:
                    return save("reconcile_required", "发现接续消息但缺少对应操作记录，需要核对来源")
                manager.store.finish_operation(continuation_id, "completed", {
                    "reconciled": True, "turn_id": continuation["id"]})
                recovery.continuation_turn_id = continuation["id"]
                if recovery.allocation_id:
                    manager.store.commit_recovery_allocation(session_id, recovery.allocation_id)
                if continuation.get("status") in {"failed", "interrupted"}:
                    return save("blocked", "已发送的接续回合未成功结束，不重复发送：" + str(continuation.get("error") or continuation["status"]))
                evidence = [item for item in continuation.get("items", []) if (
                    item.get("type") == "commandExecution" and item.get("exitCode") == 0
                    or item.get("type") == "fileChange" and item.get("status") == "completed")]
                if evidence:
                    recovery.steps.append({"state": "progress_reconciled", "turn_id": continuation["id"], "at": manager.clock()})
                    return save("complete")
                return save("awaiting_progress")
            if operation and operation["state"] not in {"planned", "not_sent"}:
                return save("reconcile_required", "接续指令已有发送记录，但历史尚不能确认结果；不会重复发送")
            if session.progress_revision != recovery.expected_progress_revision:
                return save("cancelled", "原会话已有新的有效进展，结束旧恢复")

            candidates = [turn for turn in new_turns if any(item.get("type") == "contextCompaction" for item in turn.get("items", []))]
            if recovery.compaction_turn_id:
                candidates = [turn for turn in candidates if turn["id"] == recovery.compaction_turn_id]
            if len(candidates) != 1:
                return save("reconcile_required", "无法唯一确认本次压缩回合，不重新发起压缩")
            compact = candidates[0]
            recovery.compaction_turn_id = compact["id"]
            if compact.get("status") == "inProgress":
                return save("reconcile_required", "原压缩回合仍在运行，等待原回合完成")
            if compact.get("status") != "completed" or compact.get("error"):
                if (thread.get("model") or session.model) == recovery.compression_model:
                    await manager.change_model(session_id, recovery.original_model, client)
                return save("blocked", "原压缩回合未成功完成，不重发压缩：" + str(compact.get("error") or compact.get("status")))
            if any(turn.get("status") == "inProgress" for turn in turns):
                return save("reconcile_required", "仍有其他活动回合，暂不发送接续")
            if task.status in {"complete", "failed", "paused", "blocked"}:
                return save("reconcile_required", "任务已停止或等待处理，保留已完成压缩，不发送接续")
            if reason := budget_reason(task, manager.clock()):
                return save("blocked", reason)
            current_model = thread.get("model") or session.model
            phase = next((step["state"] for step in reversed(recovery.steps)
                          if step["state"] not in {"reconciled", "progress_reconciled"}), "planned")
            allowed_models = {recovery.compression_model}
            if phase in {"selecting_target", "continuing", "awaiting_progress"}:
                allowed_models.add(recovery.target_model)
            if current_model not in allowed_models:
                return save("cancelled", "当前模型与中断步骤不一致，保留外部选择")
            route = manager.route(recovery.target_model)
            if not route.enabled or route.purpose != "execution" or route.can_pin_backend and len(route.backend_ids) > 1:
                return save("blocked", "目标路由当前不可用于恢复接续")
            backends = [Backend.model_validate(manager.store.require("backend", key)) for key in route.backend_ids]
            if any(not manager.backend_available(backend) for backend in backends):
                return save("reconcile_required", "目标后端暂不可用，保留已完成压缩，等待恢复")
            active = [row for row in manager.store.allocations() if row["session_id"] == session_id and recovery.allocation_id and row["id"].startswith(recovery.allocation_id + "/")]
            if backends and not active:
                allocation_id = f"reconciled-{recovery.id}"
                if not manager.store.reserve_recovery(allocation_id, task.id, session_id,
                    {backend.id: backend.max_sessions for backend in backends}, task.budget.max_sessions):
                    return save("reconcile_required", "目标容量不足，保留已完成压缩，等待名额")
                recovery.allocation_id = allocation_id
            # 等待核对期间，人工控制优先；接续工具内部还会重新检查范围和预算。
            recovery.steps.append({"state": "reconciled", "turn_id": compact["id"], "at": manager.clock()})
            if manager.session(session_id).control_revision != recovery.expected_control_revision:
                return save("cancelled", "核对期间出现人工操作，取消旧恢复")
            manager.validate_recovery_continuation(recovery)
            recovery.state = "continuing"
            recovery.steps.append({"state": "continuing", "at": manager.clock()})
            manager.save("recovery", recovery)
            await manager.change_model(session_id, recovery.target_model, client)
            await manager.continue_recovery(recovery, client)
            result = manager.save("recovery", recovery)
            manager._finish_recovery_on_progress(manager.session(session_id))
            return manager.store.require("recovery", result["id"])
        except asyncio.CancelledError:
            save("reconcile_required", "核对过程被中断，保留已发送操作，等待下一次核对")
            raise
        except (ConnectionError, TimeoutError) as exc:
            operation = manager.store.get_operation(f"recovery-{recovery.id}-continue")
            if operation and operation["state"] == "sent":
                manager.store.finish_operation(operation["id"], "uncertain", {"error": str(exc)})
            return save("reconcile_required", str(exc))
        except Conflict as exc:
            return save("blocked", str(exc))
        except RpcError as exc:
            return save("reconcile_required", "daemon 无法提供核对证据：" + str(exc))
