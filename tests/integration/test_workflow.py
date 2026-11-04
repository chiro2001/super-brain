from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from super_brain.api import create_app
from super_brain.manager import Manager
from super_brain.models import (
    Backend,
    Budget,
    Host,
    InferenceConfig,
    ModelRoute,
    ServiceConfig,
    Task,
)
from super_brain.store import Conflict, Store
from super_brain.worker import worker_config
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel, function_call
from tests.test_api import serve_app

pytestmark = pytest.mark.integration


async def test_work_task_tree_priority_interrupt_resume_and_parent_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "isolated-workflow-token")
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 localhost 测试主机密钥")
    names = ["A1", "A2", "A3", "B1", "C1", "D1"]
    for name in names:
        await asyncio.to_thread((tmp_path / name).mkdir)
    mock = MockModel()
    a1_started, a1_release = asyncio.Event(), asyncio.Event()
    b1_started, b1_release = asyncio.Event(), asyncio.Event()
    calls = {name: 0 for name in names}
    request_order = []
    async def handler(body):
        payload = json.dumps(body.get("input", []), ensure_ascii=False)
        name = next(name for name in names if "WORKFLOW-" + name in payload)
        calls[name] += 1
        request_order.append(name)
        if name == "A1" and calls[name] == 1:
            a1_started.set()
            await a1_release.wait()
            return None
        if name == "B1" and calls[name] == 1:
            b1_started.set()
            await b1_release.wait()
        if calls[name] == (2 if name == "A1" else 1):
            return function_call("exec_command", {"cmd": f"printf '{name}\\n' > artifact.txt", "workdir": str(tmp_path / name)})
        return None
    mock.handler = handler
    async with mock.serve() as upstream:
        daemon, sock = None, tmp_path / "control.sock"
        config = ServiceConfig(database=":memory:", poll_interval=1, auto_dispatch=False,
            hosts=[Host(id="h", name="多目标执行端", address="localhost", control_socket=str(sock),
                codex_command=[codex_binary()], known_hosts_file=str(known), allowed_roots=[str(tmp_path)])],
            backends=[Backend(id="shared", name="共享推理池", health="available", max_sessions=2, max_requests=1)],
            routes=[ModelRoute(id="a2", name="A2-dsv41f", backend_ids=["shared"], upstream_model="deepseek-flash")],
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:", max_concurrent_requests=1))
        manager = Manager(config, Store(":memory:"))
        evidence = {}
        try:
            async with serve_app(create_app(config, manager)) as url:
                config.inference.public_url = url
                daemon, sock = await start_server(tmp_path, url + "/inference/v1",
                    config_text=worker_config(url + "/inference/v1", manager.gateway.worker_token("h"), "A2-dsv41f"))
                await asyncio.wait_for(manager.host_ready.setdefault("h", asyncio.Event()).wait(), 15)
                for parent in ["A", "B", "C", "D"]:
                    manager.create_task(Task(id=parent, title="上司工作任务 " + parent, acceptance="全部所属子任务验收通过"))
                for name in names:
                    manager.create_task(Task(id=name, title="WORKFLOW-" + name, parent_id=name[0],
                        objective="在指定目录生成 artifact.txt，内容为本任务编号并实际验证。", host_id="h",
                        cwd=str(tmp_path / name), allowed_roots=[str(tmp_path / name)], allowed_models=["A2-dsv41f"],
                        dependencies=["A1"] if name == "A2" else ["A1", "A2"] if name == "A3" else [],
                        priority=10 if name in {"A2", "A3"} else 0, budget=Budget(max_requests=10, max_tokens=300000)))
                first = await manager.dispatch("A1")
                await asyncio.wait_for(a1_started.wait(), 15)
                sid = first["session_id"]
                async with asyncio.timeout(10):
                    while not manager.session(sid).active_turn_id:  # noqa: ASYNC110
                        await asyncio.sleep(.02)
                stop_cursor = manager.store.cursor()
                manager.update_task("A1", {"status": "paused"}, actor="user")
                await manager.command(sid, "interrupt", {"turn_id": manager.session(sid).active_turn_id}, "workflow-interrupt", actor="user")
                await manager.wait_codex_event(sid, stop_cursor, lambda event:event["method"] == "turn/completed")
                a1_release.set()
                for name in ["A2", "A3", "D1"]:
                    manager.update_task(name, {"status": "ready"}, actor="user")
                manager.update_task("B1", {"status": "ready", "priority": 100}, actor="user")
                config.auto_dispatch = True
                await asyncio.wait_for(b1_started.wait(), 15)
                assert request_order[:2] == ["A1", "B1"]
                assert not any(session["task_id"] in {"A2", "A3", "D1"} for session in manager.store.list("session"))
                manager.update_task("A1", {"status": "running"}, actor="user")
                resumed = await manager.command(sid, "message", {"text": "RESUME-WORKFLOW-A1：继续原任务，在原目录生成并验证产物。"}, "workflow-resume", actor="user")
                async with asyncio.timeout(10):
                    while not any(event["kind"] == "inference.queued" for event in manager.store.events(0, 1000, "A1")) or manager.session(sid).active_turn_id != resumed["result"]["turn"]["id"]:  # noqa: ASYNC110
                        await asyncio.sleep(.02)
                assert calls["A1"] == 1, "共享请求繁忙时，恢复工作应等待名额"
                queued_turn = resumed["result"]["turn"]["id"]
                stop_cursor = manager.store.cursor()
                manager.update_task("A1", {"status": "paused"}, actor="user")
                await manager.command(sid, "interrupt", {"turn_id": queued_turn}, "workflow-interrupt-queued", actor="user")
                await manager.wait_codex_event(sid, stop_cursor, lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == queued_turn)
                manager.update_task("A1", {"status": "running"}, actor="user")
                resumed = await manager.command(sid, "message", {"text": "FINAL-RESUME-WORKFLOW-A1：继续同一任务并验证产物。"}, "workflow-final-resume", actor="user")
                async with asyncio.timeout(10):
                    while sum(event["kind"] == "inference.queued" for event in manager.store.events(0, 1000, "A1")) < 2:  # noqa: ASYNC110
                        await asyncio.sleep(.02)
                assert calls["A1"] == 1 and manager.task("A1").requests_used == 1
                b1_release.set()

                async def finish_and_verify(name):
                    async with asyncio.timeout(25):
                        while True:
                            sessions = [session for session in manager.store.list("session") if session["task_id"] == name]
                            if sessions:
                                session = sessions[0]
                                records = manager.store.decision_events(session["id"])
                                finished = [event for event in records if event["body"]["method"] == "turn/completed" and event["body"]["params"]["turn"]["status"] == "completed"]
                                proofs = [event["seq"] for event in records if event["body"].get("params", {}).get("item", {}).get("type") == "commandExecution" and event["body"]["params"]["item"].get("exitCode") == 0]
                                if finished and proofs:
                                    break
                            await asyncio.sleep(.03)
                    assert (await asyncio.to_thread((tmp_path / name / "artifact.txt").read_text)).strip() == name
                    evidence[name] = proofs
                    manager.verify_task(name, True, name + " 产物内容与实际工具执行均通过", event_ids=proofs, actor="manager")
                    return session

                a1, _ = await asyncio.gather(finish_and_verify("A1"), finish_and_verify("B1"))
                assert a1["id"] == sid and a1["thread_id"] == sid.split(":", 1)[1]
                assert resumed["state"] == "completed"
                assert manager.task("A3").status in {"ready", "waiting_resource"}
                await asyncio.gather(finish_and_verify("A2"), finish_and_verify("D1"))
                await finish_and_verify("A3")
                for parent, leaves in [("A", ["A1", "A2", "A3"]), ("B", ["B1"]), ("D", ["D1"])]:
                    manager.verify_task(parent, True, "全部子任务的实际产物和验证结果已核对", actor="manager", event_ids=[seq for name in leaves for seq in evidence[name]])
                with pytest.raises(Conflict, match="子任务"):
                    manager.verify_task("C", True, "不能借用其他任务证据提前完成", event_ids=evidence["A1"], actor="manager")
                snapshot = manager.snapshot()
                assert {task["id"]: task["completed_child_task_count"] for task in snapshot["tasks"] if task["id"] in {"A", "B", "C", "D"}} == {"A": 3, "B": 1, "C": 0, "D": 1}
                assert not snapshot["recoveries"]
                assert all(session["model"] == "A2-dsv41f" for session in snapshot["sessions"])
                assert len([session for session in snapshot["sessions"] if session["task_id"] == "A1"]) == 1
                assert "子任务已验收 3/3" in manager.report()
                report = Path(".runtime/reports/workflow.json")
                await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(report.write_text, json.dumps({"passed": True, "native_codex": True, "real_models": False,
                    "request_order": request_order, "resumed_same_session": True, "urgent_before_normal": True,
                    "interruptions": 2,
                    "parents_complete": ["A", "B", "D"], "parent_C": manager.task("C").status,
                    "verified_child_artifacts": sorted(evidence), "compactions": 0}, ensure_ascii=False, indent=2))
        finally:
            a1_release.set()
            b1_release.set()
            manager.store.close()
            await stop_server(daemon)
