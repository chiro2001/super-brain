from __future__ import annotations

import pytest

from super_brain.manager import Manager
from super_brain.models import ServiceConfig, Session, Task
from super_brain.store import Conflict, Store


def test_parent_requires_verified_children_and_can_use_their_actual_evidence():
    manager = Manager(ServiceConfig(), Store(":memory:"))
    try:
        manager.create_task(Task(id="A", title="交付 A", acceptance="全部子任务验收通过"))
        for name in ["A1", "A2"]:
            manager.create_task(Task(id=name, title=name, parent_id="A"))
            manager.save("session", Session(id=f"h:{name}", host_id="h", thread_id=name, task_id=name))
            manager.ingest("h", {"method": "item/completed", "params": {"threadId": name, "turnId": name,
                "item": {"id": "verify-" + name, "type": "commandExecution", "command": "pytest", "exitCode": 0, "aggregatedOutput": "1 passed"}}})
        events = {name: next(event["seq"] for event in manager.store.decision_events(f"h:{name}") if event["body"]["method"] == "item/completed") for name in ["A1", "A2"]}
        manager.verify_task("A1", True, "A1 已验收", event_ids=[events["A1"]], actor="manager")
        with pytest.raises(Conflict, match="子任务"):
            manager.verify_task("A", True, "不能提前交付", event_ids=list(events.values()), actor="manager")
        manager.verify_task("A2", True, "A2 已验收", event_ids=[events["A2"]], actor="manager")
        result = manager.verify_task("A", True, "全部子任务实际验证通过", event_ids=list(events.values()), actor="manager")
        assert result["status"] == "complete"
        parent = next(task for task in manager.snapshot()["tasks"] if task["id"] == "A")
        assert parent["completed_child_task_count"] == parent["child_task_count"] == 2
        assert "子任务已验收 2/2" in manager.report()
    finally:
        manager.store.close()


def test_parent_relationship_cannot_form_a_cycle_or_point_to_missing_task():
    manager = Manager(ServiceConfig(), Store(":memory:"))
    try:
        manager.create_task(Task(id="A", title="A"))
        manager.create_task(Task(id="A1", title="A1", parent_id="A"))
        with pytest.raises(Conflict, match="父子任务"):
            manager.update_task("A", {"parent_id": "A1"}, actor="user")
        with pytest.raises(Conflict, match="父任务不存在"):
            manager.update_task("A1", {"parent_id": "missing"}, actor="user")
        assert manager.task("A").parent_id is None
    finally:
        manager.store.close()


def test_scope_changes_and_new_children_reopen_previously_verified_parents():
    manager = Manager(ServiceConfig(), Store(":memory:"))
    try:
        manager.create_task(Task(id="A", title="A"))
        manager.create_task(Task(id="A1", title="A1", parent_id="A"))
        manager.verify_task("A1", True, "原验收通过")
        manager.verify_task("A", True, "原父任务验收通过")
        manager.update_task("A1", {"objective": "用户明确改变了原目标"}, actor="user")
        assert manager.task("A1").status == "planned" and manager.task("A1").verification is None
        assert manager.task("A").status == "planned" and manager.task("A").verification is None
        manager.verify_task("A1", True, "新目标已验收")
        manager.verify_task("A", True, "重新验收父任务")
        manager.create_task(Task(id="A2", title="新子任务", parent_id="A"))
        assert manager.task("A").status == "planned" and manager.task("A").verification is None
        manager.verify_task("A", True, "人工确认")
        manager.verify_task("A1", False, "后续复查失败")
        assert manager.task("A").status == "planned" and manager.task("A").verification is None
    finally:
        manager.store.close()


@pytest.mark.parametrize("reason", ["dependency", "capacity"])
async def test_waiting_tasks_do_not_repeat_remote_directory_checks(monkeypatch, reason):
    from super_brain.models import Budget, Host
    manager = Manager(ServiceConfig(hosts=[Host(id="h", name="主机", address="localhost", allowed_roots=["/work"])]), Store(":memory:"))
    class Client:
        connected = True
    manager.clients["h"] = Client()
    async def should_not_connect(*args):
        pytest.fail("依赖未完成或容量不足时，不应启动 SSH 路径核查")
    monkeypatch.setattr("super_brain.manager.check_remote_scope", should_not_connect)
    try:
        manager.create_task(Task(id="A", title="前置任务"))
        manager.create_task(Task(id="B", title="待执行", host_id="h", cwd="/work/b", allowed_roots=["/work"],
            dependencies=["A"] if reason == "dependency" else [], budget=Budget(max_requests=5)))
        if reason == "dependency":
            with pytest.raises(Conflict, match="前置任务"):
                await manager.dispatch("B")
        else:
            assert (await manager.dispatch("B"))["status"] == "waiting_resource"
    finally:
        manager.store.close()


async def test_cancelled_dispatch_releases_reservation_before_creating_work(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from super_brain.models import Backend, Budget, Host, InferenceConfig, ModelRoute
    config = ServiceConfig(hosts=[Host(id="h", name="主机", address="localhost", allowed_roots=["/work"])],
        backends=[Backend(id="pool", name="池", health="available")],
        routes=[ModelRoute(id="a2", name="A2", backend_ids=["pool"])],
        inference=InferenceConfig(enabled=True, public_url="http://localhost"))
    manager = Manager(config, Store(":memory:"))
    manager.gateway = SimpleNamespace(token=lambda _: "test-task-token")
    waiting = asyncio.Event()
    class Client:
        connected = True
        async def call(self, method, params):
            assert method == "config/read", "取消前不得发出工作"
            waiting.set()
            await asyncio.Event().wait()
    async def allowed(*args):
        return {"allowed": True}
    monkeypatch.setattr("super_brain.manager.check_remote_scope", allowed)
    manager.clients["h"] = Client()
    manager.create_task(Task(id="A", title="等待分配", host_id="h", cwd="/work/a", allowed_roots=["/work"], budget=Budget(max_requests=1)))
    work = asyncio.create_task(manager.dispatch("A"))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert manager.store.allocations()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert not manager.store.allocations()
        assert manager.task("A").status == "planned" and not manager.store.list("session")
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        manager.store.close()


def test_changed_task_requires_new_execution_evidence_for_automatic_verification():
    manager = Manager(ServiceConfig(), Store(":memory:"))
    try:
        manager.create_task(Task(id="A", title="目标调整"))
        manager.save("session", Session(id="h:t", host_id="h", thread_id="t", task_id="A"))
        def execute(item_id):
            manager.ingest("h", {"method": "item/completed", "params": {"threadId": "t", "turnId": item_id,
                "item": {"id": item_id, "type": "commandExecution", "exitCode": 0, "command": "pytest"}}})
            return manager.store.completed_item_seq("h:t", item_id)
        old = execute("old")
        manager.verify_task("A", True, "旧目标验证", event_ids=[old], actor="manager")
        manager.update_task("A", {"objective": "新的验收目标"}, actor="user")
        with pytest.raises(Conflict, match="实际成功命令"):
            manager.verify_task("A", True, "旧证据不能用于新目标", event_ids=[old], actor="manager")
        fresh = execute("new")
        assert manager.verify_task("A", True, "重新核验新目标", event_ids=[fresh], actor="manager")["status"] == "complete"
    finally:
        manager.store.close()
