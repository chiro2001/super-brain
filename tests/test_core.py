from __future__ import annotations

import pytest

from super_brain.manager import Manager
from super_brain.models import (
    Backend,
    Budget,
    Host,
    ModelRoute,
    ServiceConfig,
    Session,
    StallPolicy,
    Task,
)
from super_brain.policy import assess_stall, within_scope
from super_brain.store import Conflict, Store


@pytest.fixture
def manager():
    store = Store(":memory:")
    result = Manager(ServiceConfig(), store, clock=lambda: 5000)
    yield result
    store.close()


def test_work_dependencies_scope_and_verification(manager):
    """TASK-01/02/04：实际任务图与验收，不用会话闲置代替完成。"""
    a = manager.create_task(Task(id="A", title="交付接口", cwd="/work/a", allowed_roots=["/work"]))
    b = manager.create_task(Task(id="B", title="集成", dependencies=[a["id"]], budget=Budget(max_requests=3)))
    assert manager.select_route(Task.model_validate(b)) is None
    with pytest.raises(Conflict, match="验收证据"):
        manager.update_task("A", {"status": "complete"})
    assert manager.verify_task("A", False, "集成测试失败")["status"] == "blocked"
    assert manager.verify_task("A", True, "接口测试通过，产物已检查")["status"] == "complete"
    with pytest.raises(Conflict, match="形成环"):
        manager.update_task("A", {"dependencies": ["B"]}, actor="user")


def test_scope_change_requires_user_and_preserves_original(manager):
    """AUTH-02：管理 agent 不能自行扩大目录与预算。"""
    manager.create_task(Task(id="A", title="任务", cwd="/work/a", allowed_roots=["/work/a"]))
    result = manager.update_task("A", {"allowed_roots": ["/"]})
    assert result["approval_required"]
    assert manager.task("A").allowed_roots == ["/work/a"]
    assert manager.update_task("A", {"next_step": "在原目录内修复失败测试"})["next_step"]
    with pytest.raises(Conflict, match="目录越出"):
        manager.update_task("A", {"cwd": "/outside"}, actor="user")


@pytest.mark.parametrize("path", ["/work/a/../outside", "relative", "/work-ab/secret"])
def test_directory_escape_is_rejected(path):
    assert not within_scope(path, ["/work/a"])


def test_revision_and_restart_preserve_task_progress(tmp_path):
    """TASK-03/DURABLE-01：重启不丢任务，过期修改被拒绝。"""
    db = str(tmp_path / "state.db")
    first = Manager(ServiceConfig(database=db))
    original = first.create_task(Task(id="A", title="待继续"))
    first.update_task("A", {"next_step": "继续验证"}, expected=original["revision"])
    with pytest.raises(Conflict, match="状态已变化"):
        first.update_task("A", {"next_step": "过期修改"}, expected=original["revision"])
    cursor = first.store.cursor()
    first.store.close()
    second = Manager(ServiceConfig(database=db))
    assert second.task("A").next_step == "继续验证"
    assert second.store.cursor() == cursor
    second.store.close()


def test_aliases_share_capacity_and_opaque_zl_adds_no_capacity(manager):
    """RES-01/03/04：别名不增加物理容量，未知入口不冒充可用后端。"""
    manager.save("backend", Backend(id="a3", name="a3", health="available", max_sessions=1))
    manager.save("backend", Backend(id="unknown", name="不可用实例"))
    for name in ["A3-dsv41f", "alias-a3"]:
        manager.save("route", ModelRoute(id=name, name=name, backend_ids=["a3"]))
    manager.save("route", ModelRoute(id="bad", name="A3-dsv41f-2", backend_ids=["unknown"]))
    manager.save("route", ModelRoute(id="zl", name="ZL-DeepSeek-v4.1", opaque=True, purpose="recovery"))
    task = Task(id="A", title="新工作", budget=Budget(max_requests=10))
    assert manager.select_route(task)["backend_id"] == "a3"
    assert manager.store.reserve("lease-1", "other", "a3", 1, 1)
    assert manager.select_route(task) is None
    assert not manager.store.reserve("lease-2", "A", "a3", 1, 1)
    manager.store.release("lease-1")
    assert manager.select_route(task)


def stalled_session(**kwargs):
    defaults = dict(id="s", host_id="h", thread_id="t", connected=True, status="active",
                    last_progress_at=100, last_activity_at=4900,
                    stall_policy=StallPolicy(enabled=True, suspect_after=900, confirm_after=1800),
                    diagnosis={"cause": "model_backend", "confirmed": True,
                               "progress_revision": 0, "at": 4000, "reason": "后端请求长期未返回"})
    return Session(**{**defaults, **kwargs})


def test_slow_progress_is_never_migrated_even_with_high_load():
    """STALL-01/02：有进展就保持当前后端，负载不是迁移触发器。"""
    result = assess_stall(stalled_session(last_progress_at=4900), 5000)
    assert result["state"] == "running" and not result["eligible"]
    streaming = assess_stall(stalled_session(last_output_at=4990), 5000)
    assert streaming["state"] == "slow_streaming" and not streaming["eligible"]


@pytest.mark.parametrize("patch,state", [
    ({"connected": False}, "observation_lost"),
    ({"waiting_reason": "需要补充信息"}, "waiting"),
    ({"tool_running": True}, "tool_running"),
    ({"diagnosis": None}, "suspected_stall"),
    ({"progress_revision": 1}, "suspected_stall"),
])
def test_no_silent_stall_assumptions(patch, state):
    """STALL-03/05：耗时、断线和正常等待均不能单独触发迁移。"""
    result = assess_stall(stalled_session(**patch), 5000)
    assert result["state"] == state and not result["eligible"]


def test_confirmed_stall_requires_fresh_diagnosis_and_can_be_cancelled(manager):
    session = stalled_session()
    assert assess_stall(session, 5000)["eligible"]
    manager.save("session", session)
    manager.mark_progress("s", "出现新的有效验证结果")
    assert not assess_stall(manager.session("s"), 5000)["eligible"]


@pytest.mark.parametrize("status", ["unloaded", "notLoaded", "unknown", "usageLimited", "systemError"])
def test_inactive_or_unconfirmed_threads_are_never_migrated(status):
    result = assess_stall(stalled_session(status=status), 5000)
    assert result["state"] == status and not result["eligible"]


async def test_human_control_and_durable_idempotency(manager):
    """AUTH-03/CODEX-05：重复发送不重复执行，过期 turn 不被中断。"""
    class Client:
        connected = True
        calls = []
        async def call(self, method, params):
            self.calls.append((method, params))
            return {"ok": True}
    client = Client()
    manager.clients["h"] = client
    manager.save("session", Session(id="s", host_id="h", thread_id="t", active_turn_id="turn-new"))
    await manager.command("s", "message", {"text": "继续"}, "same-id", actor="user")
    await manager.command("s", "message", {"text": "继续"}, "same-id", actor="user")
    assert len(client.calls) == 1
    assert manager.session("s").control_revision == 1
    with pytest.raises(Conflict, match="回合已变化"):
        await manager.command("s", "interrupt", {"turn_id": "turn-old"}, "interrupt-id", actor="user")
    assert len(client.calls) == 1


async def test_recovery_cannot_be_triggered_just_because_a_session_is_slow(manager):
    manager.save("session", stalled_session(last_progress_at=4999))
    with pytest.raises(Conflict, match="不满足停滞恢复条件"):
        await manager.recover("s", "A3-dsv41f")


def test_host_rejects_ssh_option_injection():
    with pytest.raises(ValueError):
        Host(name="h", address="-oProxyCommand=bad", control_socket="/tmp/a")


def test_configuration_updates_existing_routes_and_stale_metrics_deny_new_work(tmp_path):
    path = str(tmp_path / "config-state.db")
    backend = Backend(id="a", name="实例", health="available", metrics_url="http://test/metrics", observed_at=100)
    route = ModelRoute(id="a", name="A", backend_ids=["a"])
    config = ServiceConfig(database=path, backends=[backend], routes=[route])
    first = Manager(config, clock=lambda: 5000)
    task = Task(title="新工作", budget=Budget(max_requests=1))
    assert first.select_route(task) is None
    backend.observed_at = 4999
    first.save("backend", backend)
    assert first.select_route(task)["model"] == "A"
    first.store.close()
    config.routes[0].enabled = False
    second = Manager(config, clock=lambda: 5000)
    assert not second.route("A").enabled and second.select_route(task) is None
    second.store.close()


def test_only_one_coordinator_can_open_database(tmp_path):
    from filelock import Timeout
    path = str(tmp_path / "single.db")
    first = Store(path)
    try:
        with pytest.raises(Timeout):
            Store(path)
    finally:
        first.close()
    second = Store(path)
    second.close()
