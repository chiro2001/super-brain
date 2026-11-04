from __future__ import annotations

import pytest

from super_brain.manager import Manager
from super_brain.models import Budget, ServiceConfig, Session, Task
from super_brain.store import Conflict, Store
from super_brain.supervisor import Decision, Supervisor, queue_decision


def make_manager():
    manager = Manager(ServiceConfig(), Store(":memory:"), clock=lambda: 5000)
    manager.create_task(Task(id="A", title="修复接口", cwd="/work/a", allowed_roots=["/work/a"],
                             budget=Budget(max_requests=10, max_retries=1)))
    manager.save("session", Session(id="h:t", host_id="h", thread_id="t", task_id="A",
                                    cwd="/work/a", model="A2", connected=True, control_enabled=True))
    return manager


async def test_supervisor_repairs_within_scope_and_stops_at_retry_limit():
    manager = make_manager()
    class Client:
        connected = True
        calls = []
        async def call(self, method, params):
            self.calls.append((method, params))
            return {"ok": True}
    manager.clients["h"] = Client()
    class Driver(Supervisor):
        async def decide(self, job):
            return Decision(action="repair", reason="原目录内测试失败", message="修复当前接口测试并重新验证，不改变验收要求。")
    queue_decision(manager, manager.session("h:t"), "测试失败", "error", 0)
    first = manager.store.list("decision")[0]
    assert (await Driver(manager).process(first["id"]))["state"] == "complete"
    assert manager.task("A").retries_used == 1
    assert len(manager.clients["h"].calls) == 1
    assert manager.task("A").allowed_roots == ["/work/a"]
    queue_decision(manager, manager.session("h:t"), "仍然失败", "error", 0)
    second = next(job for job in manager.store.list("decision") if job["state"] == "pending")
    assert (await Driver(manager).process(second["id"]))["state"] == "blocked"
    assert len(manager.clients["h"].calls) == 1
    manager.store.close()


async def test_new_progress_invalidates_an_inflight_supervisor_decision():
    manager = make_manager()
    class Driver(Supervisor):
        async def decide(self, job):
            manager.mark_progress("h:t", "原会话已自行完成修复")
            return Decision(action="repair", reason="过期判断", message="不应发送")
    queue_decision(manager, manager.session("h:t"), "疑似失败", "error", 0)
    job = manager.store.list("decision")[0]
    assert (await Driver(manager).process(job["id"]))["state"] == "resolved"
    assert manager.task("A").retries_used == 0
    manager.store.close()


def test_automatic_completion_requires_actual_matching_tool_evidence():
    manager = make_manager()
    with pytest.raises(Conflict, match="实际成功命令"):
        manager.verify_task("A", True, "模型自述完成", actor="manager")
    manager.ingest("h", {"method": "item/completed", "params": {"threadId": "t",
        "item": {"type": "commandExecution", "exitCode": 0, "command": "pytest", "aggregatedOutput": "1 passed"}}})
    seq = next(e["seq"] for e in manager.store.events(0, 1000, "h:t") if e["kind"] == "codex.event")
    assert manager.verify_task("A", True, "pytest 通过", actor="manager", event_ids=[seq])["status"] == "complete"
    manager.store.close()


def test_decision_evidence_survives_thousands_of_text_deltas():
    manager = make_manager()
    manager.ingest("h", {"method": "item/completed", "params": {"threadId": "t",
        "item": {"id": "tested", "type": "commandExecution", "exitCode": 0, "command": "pytest", "aggregatedOutput": "1 passed"}}})
    evidence = next(event["seq"] for event in manager.store.decision_events("h:t") if event["body"]["method"] == "item/completed")
    with manager.store.transaction():
        for _ in range(2000):
            manager.ingest("h", {"method": "item/agentMessage/delta", "params": {"threadId": "t", "itemId": "text", "delta": "更多说明"}})
    assert any(event["seq"] == evidence for event in manager.store.decision_events("h:t"))
    assert manager.verify_task("A", True, "实际测试通过", actor="manager", event_ids=[evidence])["status"] == "complete"
    manager.store.close()


async def test_failed_automatic_verification_queues_budgeted_repair():
    manager = make_manager()
    class Client:
        connected = True
        async def call(self, method, params):
            assert method == "turn/start"
            return {"ok": True}
    manager.clients["h"] = Client()
    class Driver(Supervisor):
        async def decide(self, job):
            return Decision(action="repair", reason="验收失败后继续原任务", message="修复失败的测试并重新验证。")
    manager.verify_task("A", False, "测试仍失败", actor="manager")
    job = next(job for job in manager.store.list("decision") if job["source"] == "verification_failed")
    result = await Driver(manager).process(job["id"])
    assert result["state"] == "complete"
    assert manager.task("A").status == "running" and manager.task("A").retries_used == 1
    manager.store.close()


@pytest.mark.parametrize("change", ["objective", "pause", "complete"])
async def test_inflight_decision_cannot_override_task_changes(change):
    manager = make_manager()
    class Driver(Supervisor):
        async def decide(self, job):
            if change == "objective":
                manager.update_task("A", {"objective": "用户已修改目标"}, actor="user")
            elif change == "pause":
                manager.update_task("A", {"status": "paused"}, actor="user")
            else:
                manager.verify_task("A", True, "用户已核对并完成")
            return Decision(action="repair", reason="旧上下文中的修复", message="不能继续执行旧工作")
    queue_decision(manager, manager.session("h:t"), "原问题", "error", 0)
    try:
        result = await Driver(manager).process(manager.store.list("decision")[0]["id"])
        assert result["state"] == "resolved" and manager.task("A").retries_used == 0
    finally:
        manager.store.close()


async def test_repair_invalidates_other_queued_decisions_from_same_retry():
    manager = make_manager()
    calls = []
    class Client:
        connected = True
        async def call(self, method, params):
            calls.append(method)
            return {"ok": True}
    manager.clients["h"] = Client()
    decisions = []
    class Driver(Supervisor):
        async def decide(self, job):
            decisions.append(job["id"])
            return Decision(action="repair", reason="只安排一次原任务修复", message="修复并验证")
    queue_decision(manager, manager.session("h:t"), "工具失败", "error", 0)
    queue_decision(manager, manager.session("h:t"), "回合结束", "verify", 0)
    jobs = manager.store.list("decision")
    try:
        driver = Driver(manager)
        assert (await driver.process(jobs[0]["id"]))["state"] == "complete"
        assert (await driver.process(jobs[1]["id"]))["state"] == "resolved"
        assert len(decisions) == len(calls) == 1
    finally:
        manager.store.close()
