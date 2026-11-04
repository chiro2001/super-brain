from __future__ import annotations

import asyncio

import pytest

from super_brain.manager import Manager
from super_brain.models import Budget, ModelRoute, ServiceConfig, Session, StallPolicy, Task
from super_brain.store import Conflict, Store


@pytest.fixture
def manager():
    result = Manager(ServiceConfig(), Store(":memory:"), clock=lambda: 5000)
    result.create_task(Task(id="A", title="原任务", cwd="/work/a", allowed_roots=["/work/a"],
                            budget=Budget(max_requests=10, max_retries=2)))
    result.save("session", Session(id="h:t", host_id="h", thread_id="t", task_id="A",
        cwd="/work/a", model="A2", connected=True, control_enabled=True, status="active",
        last_progress_at=100, last_activity_at=4900,
        stall_policy=StallPolicy(enabled=True, suspect_after=900, confirm_after=1800),
        diagnosis={"cause": "model_backend", "confirmed": True, "progress_revision": 0,
                   "at": 4000, "reason": "后端请求长期无响应"}))
    for name, purpose in [("A3", "execution"), ("ZL-DeepSeek-v4.1", "recovery")]:
        result.save("route", ModelRoute(id=name, name=name, purpose=purpose, opaque=True))
    yield result
    result.store.close()


async def test_manual_message_cancels_long_compaction_and_restores_model(manager):
    started = asyncio.Event()

    class Client:
        connected = True
        calls = []

        async def call(self, method, params):
            self.calls.append((method, params))
            if method == "thread/read":
                return {"thread": {"id": "t", "turns": []}}
            if method == "thread/settings/update":
                manager.ingest("h", {"method": "thread/settings/updated", "params": {
                    "threadId": "t", "threadSettings": {"model": params["model"], "cwd": "/work/a"}}})
            if method == "thread/compact/start":
                manager.ingest("h", {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "compact"}}})
                manager.ingest("h", {"method": "item/started", "params": {"threadId": "t", "turnId": "compact", "item": {"type": "contextCompaction"}}})
                started.set()
            if method == "turn/interrupt":
                manager.ingest("h", {"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "compact", "status": "interrupted"}}})
            return {"ok": True}

    client = Client()
    manager.clients["h"] = client
    recovery = asyncio.create_task(manager.recover("h:t", "A3"))
    await asyncio.wait_for(started.wait(), 2)
    manual = asyncio.create_task(manager.command("h:t", "message", {"text": "按我的新指令继续"}, "manual", actor="user"))
    result, operation = await asyncio.wait_for(asyncio.gather(recovery, manual), 3)
    assert result["state"] == "cancelled"
    assert operation["state"] == "completed"
    assert manager.session("h:t").model == "A2"
    assert any(method == "turn/interrupt" for method, _ in client.calls)
    assert [params["input"][0]["text"] for method, params in client.calls if method == "turn/start"] == ["按我的新指令继续"]
    assert sum(method == "thread/compact/start" for method, _ in client.calls) == 1


async def test_stale_automatic_command_rejected_and_budget_exhausted_stop_allowed(manager):
    class Client:
        connected = True
        calls = []
        async def call(self, method, params):
            self.calls.append((method, params))
            return {"ok": True}
    client = Client()
    manager.clients["h"] = client
    current = manager.session("h:t")
    current.control_revision = 2
    current.active_turn_id = "active"
    manager.save("session", current)
    with pytest.raises(Conflict, match="过期"):
        await manager.command("h:t", "message", {"text": "旧指令"}, "stale", expected_control_revision=1)
    task = manager.task("A")
    task.requests_used = task.budget.max_requests
    manager.save("task", task)
    await manager.command("h:t", "interrupt", {"turn_id": "active"}, "stop", expected_control_revision=2)
    assert [method for method, _ in client.calls] == ["turn/interrupt"]


async def test_slow_poll_does_not_overwrite_new_external_settings_or_control(manager):
    clock = [5000]
    manager.clock = lambda: clock[0]
    class Client:
        async def call(self, method, params):
            if method == "thread/loaded/list":
                return {"data": ["t"]}
            if method == "thread/read":
                clock[0] += 1
                manager.ingest("h", {"method": "thread/settings/updated", "params": {"threadId": "t", "threadSettings": {"model": "user-model", "cwd": "/work/a"}}})
                current = manager.session("h:t")
                current.control_enabled = False
                manager.save("session", current)
                return {"thread": {"id": "t", "model": "stale-model", "cwd": "/stale", "status": {"type": "idle"}}}
            return {"goal": None}
    await manager.sync_host("h", Client(), {"t"})
    current = manager.session("h:t")
    assert current.model == "user-model" and current.cwd == "/work/a"
    assert current.control_revision == 1 and not current.control_enabled


def test_history_recovery_is_idempotent_and_preserves_progress_time(manager):
    history = {"id": "t", "turns": [{"id": "past", "status": "completed", "items": [
        {"id": "past-command", "type": "commandExecution", "exitCode": 0, "command": "pytest", "aggregatedOutput": "1 passed"},
        {"id": "past-message", "type": "agentMessage", "text": "原结果"}]}]}
    manager.backfill_history("h", history)
    manager.backfill_history("h", history)
    manager.ingest("h", {"method": "item/completed", "params": {"threadId": "t", "turnId": "past", "item": history["turns"][0]["items"][0]}})
    recovered = [event for event in manager.store.events(0, 1000, "h:t") if event["kind"] == "codex.event"]
    assert len(recovered) == 2 and all(event["body"]["historyRecovered"] for event in recovered)
    assert manager.session("h:t").last_progress_at == 100
    assert manager.session("h:t").progress_revision == 0
    manager.verify_task("A", True, "历史中的真实测试命令", actor="manager", event_ids=[recovered[0]["seq"]])


async def test_restart_keeps_uncertain_commands_and_recovery_without_resending(tmp_path):
    path = str(tmp_path / "restart.db")
    config = ServiceConfig(database=path)
    first = Manager(config)
    first.save("session", Session(id="h:t", host_id="h", thread_id="t", connected=True))
    class Client:
        connected = True
        calls = 0
        async def call(self, method, params):
            self.calls += 1
            raise ConnectionError("模拟指令发送后 SSH 断开")
    client = Client()
    first.clients["h"] = client
    with pytest.raises(ConnectionError):
        await first.command("h:t", "message", {"text": "原指令"}, "once", actor="user")
    first.save("recovery", {"id": "recover", "session_id": "h:t", "state": "compressing", "expected_progress_revision": 0})
    first.store.close()
    second = Manager(config)
    second.clients["h"] = client
    try:
        await second.start()
        result = await second.command("h:t", "message", {"text": "原指令"}, "once", actor="user")
        assert result["state"] == "uncertain" and client.calls == 1
        assert second.store.require("recovery", "recover")["state"] == "reconcile_required"
        second.mark_progress("h:t", "核对后原会话恢复了有效进展")
        assert second.store.require("recovery", "recover")["state"] == "cancelled"
    finally:
        second.clients.clear()
        await second.stop()
        second.store.close()


async def test_offline_manual_input_and_real_tool_progress_invalidate_old_decisions(manager):
    from super_brain.supervisor import Supervisor, queue_decision
    manager.update_task("A", {"acceptance": "测试实际通过"}, actor="user")
    original_control = manager.session("h:t").control_revision
    queue_decision(manager, manager.session("h:t"), "断线前的旧判断", "error", 0)
    old = manager.store.list("decision")[0]
    manager.store.operation("owned", "h:t", "message", {"text": "原指令"})
    manager.store.finish_operation("owned", "completed", {})
    history = {"id": "t", "turns": [{"id": "offline", "status": "completed", "items": [
        {"id": "owned", "type": "userMessage", "clientId": "sb-owned", "completedAtMs": 4400000},
        {"id": "manual", "type": "userMessage", "clientId": "native-user", "completedAtMs": 4500000},
        {"id": "old", "type": "commandExecution", "exitCode": 0, "completedAtMs": 50000},
        {"id": "new", "type": "commandExecution", "exitCode": 0, "completedAtMs": 4600000},
        {"id": "answer", "type": "agentMessage", "text": "实际已推进", "completedAtMs": 4700000}]}]}
    manager.backfill_history("h", history)
    manager.backfill_history("h", history)
    current = manager.session("h:t")
    assert current.control_revision == original_control + 1 and current.progress_revision == 1
    assert current.last_progress_at == 4600 and current.last_output_at == 4700
    assert current.diagnosis is None and not manager.assess(current)["eligible"]
    class Driver(Supervisor):
        async def decide(self, job):
            pytest.fail("断线期间出现人工输入，不能继续执行旧决策")
    assert (await Driver(manager).process(old["id"]))["state"] == "resolved"
    queued = [job for job in manager.store.list("decision") if job["source"] == "verify"]
    assert len(queued) == 1 and queued[0]["control_revision"] == original_control + 1


@pytest.mark.parametrize("case,expected", [("model", 1), ("cwd", 1), ("goal", 1), ("own_model_intent", 0)])
async def test_reconnect_detects_external_settings_but_preserves_own_model_intent(manager, case, expected):
    model = "A3" if case in {"model", "own_model_intent"} else "A2"
    class Client:
        async def call(self, method, params):
            if method == "thread/loaded/list":
                return {"data": ["t"]}
            if method == "thread/read":
                return {"thread": {"id": "t", "model": model, "cwd": "/work/new" if case == "cwd" else "/work/a", "status": {"type": "idle"}}}
            if method == "thread/goal/get":
                return {"goal": {"objective": "用户改了原生目标"} if case == "goal" else None}
            return {}
    client = Client()
    if case == "own_model_intent":
        await manager.change_model("h:t", "A3", client)
    await manager.sync_host("h", client, {"t"})
    assert manager.session("h:t").control_revision == expected


async def test_automatic_message_cannot_use_a_model_outside_task_scope(manager):
    manager.update_task("A", {"allowed_models": ["A3"]}, actor="user")
    with pytest.raises(Conflict, match="当前模型不在任务允许范围"):
        await manager.command("h:t", "message", {"text": "不能继续用原 A2 模型执行"}, "wrong-model",
            expected_control_revision=manager.session("h:t").control_revision)


async def test_manager_cannot_send_regular_work_through_compaction_route(manager):
    session = manager.session("h:t")
    session.model = "ZL-DeepSeek-v4.1"
    manager.save("session", session)
    with pytest.raises(Conflict, match="仅用于压缩"):
        await manager.command("h:t", "message", {"text": "普通工作不能自动用恢复入口执行"}, "wrong-purpose")


async def test_native_compaction_invalidates_old_decisions_and_blocks_duplicate_recovery(manager):
    from super_brain.supervisor import Supervisor, queue_decision
    queue_decision(manager, manager.session("h:t"), "压缩前的判断", "error", 0)
    job = manager.store.list("decision")[0]
    manager.ingest("h", {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "native-compact"}}})
    manager.ingest("h", {"method": "item/started", "params": {"threadId": "t", "turnId": "native-compact", "item": {"id": "native-item", "type": "contextCompaction"}}})
    assert manager.session("h:t").control_revision == 1
    manager.diagnose("h:t", "model_backend", True, "即使重新给出停滞诊断也不能重复压缩")
    assert manager.assess(manager.session("h:t"))["state"] == "compacting"
    with pytest.raises(Conflict, match="不满足停滞恢复条件"):
        await manager.recover("h:t", "A3")
    with pytest.raises(Conflict, match="压缩回合尚未结束"):
        await manager.command("h:t", "message", {"text": "等待压缩完成后再工作"}, "busy-compact", expected_control_revision=1)
    assert (await Supervisor(manager).process(job["id"]))["state"] == "resolved"
    assert "原生压缩中" in manager.report()
    manager.ingest("h", {"method": "item/completed", "params": {"threadId": "t", "turnId": "native-compact", "item": {"id": "native-item", "type": "contextCompaction"}}})
    current = manager.session("h:t")
    assert current.compaction_turn_id is None and current.last_progress_at == 100
    assert manager.assess(current)["state"] == "running", "压缩后的新阶段不能立即被旧停滞时长触发再次压缩"
