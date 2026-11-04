from __future__ import annotations

import pytest

from super_brain.manager import Manager
from super_brain.models import Budget, Recovery, ServiceConfig, Session, Task
from super_brain.policy import task_scope_fingerprint
from super_brain.store import Store


@pytest.mark.parametrize("case,expected", [
    ("paused", "reconcile_required"), ("budget", "blocked"), ("scope", "cancelled"),
    ("foreign_model", "cancelled"), ("unconfirmed_send", "reconcile_required"),
    ("active_compact", "reconcile_required"), ("changed_goal", "cancelled"),
    ("legacy_baseline", "reconcile_required"),
])
async def test_reconciliation_respects_evidence_scope_budget_and_manual_choices(case, expected):
    manager = Manager(ServiceConfig(), Store(":memory:"))
    task = Task(id="A", title="原任务", status="running", cwd="/work/a", allowed_roots=["/work/a"], budget=Budget(max_requests=1))
    manager.create_task(task)
    manager.save("session", Session(id="h:t", host_id="h", thread_id="t", task_id="A", cwd="/work/a",
        model="ZL-DeepSeek-v4.1", connected=True, control_enabled=True))
    recovery = Recovery(id="r", session_id="h:t", task_id="A", original_model="A2", target_model="A3",
        state="reconcile_required", expected_progress_revision=0, expected_control_revision=0,
        baseline_turn_ids=["initial"], scope_fingerprint=task_scope_fingerprint(task), compaction_turn_id="compact",
        steps=[{"state": "compressing"}])
    if case == "paused":
        task.status = "paused"
    elif case == "budget":
        task.requests_used = 1
    elif case == "scope":
        task.objective = "用户已改目标"
    elif case == "unconfirmed_send":
        manager.store.operation("recovery-r-continue", "h:t", "message", {"text": "原接续"})
        manager.store.finish_operation("recovery-r-continue", "sent", {})
    elif case == "legacy_baseline":
        recovery.baseline_turn_ids = None
    manager.save("task", task)
    manager.save("recovery", recovery)
    class Client:
        connected = True
        async def call(self, method, params):
            if method == "thread/goal/get":
                return {"goal": {"objective": "人工新目标"} if case == "changed_goal" else None}
            assert method == "thread/read", "证据或授权不充分时不能修改设置或重发工作"
            return {"thread": {"id": "t", "model": "A2" if case == "foreign_model" else "ZL-DeepSeek-v4.1", "turns": [
                {"id": "initial", "status": "completed", "items": []},
                {"id": "compact", "status": "inProgress" if case == "active_compact" else "completed",
                 "items": [{"id": "compact-item", "type": "contextCompaction"}]}]}}
    manager.clients["h"] = Client()
    try:
        result = await manager.reconcile_recovery("r")
        assert result["state"] == expected and result["error"]
        assert not manager.store.get_operation("recovery-r-continue") or case == "unconfirmed_send"
    finally:
        manager.store.close()


def test_long_internal_turn_baseline_does_not_fill_public_agent_context():
    manager = Manager(ServiceConfig(), Store(":memory:"))
    manager.save("session", Session(id="h:t", host_id="h", thread_id="t"))
    baseline = [f"past-turn-{index}" for index in range(10000)]
    manager.save("recovery", Recovery(id="r", session_id="h:t", task_id="A", original_model="A2", target_model="A3",
        expected_progress_revision=0, expected_control_revision=0, baseline_turn_ids=baseline, scope_fingerprint="internal"))
    try:
        view = manager.snapshot()["recoveries"][0]
        assert view["baseline_turn_count"] == 10000
        assert "baseline_turn_ids" not in view and "scope_fingerprint" not in view
        event = next(event for event in manager.store.events(0, 100) if event["kind"] == "recovery.updated")
        assert event["body"]["baseline_turn_count"] == 10000 and "baseline_turn_ids" not in event["body"]
        assert manager.store.require("recovery", "r")["baseline_turn_ids"] == baseline
    finally:
        manager.store.close()
