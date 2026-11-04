from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
from super_brain.rpc import CodexClient
from super_brain.store import Store
from super_brain.supervisor import Supervisor, queue_decision
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel, function_call

pytestmark = pytest.mark.integration


async def test_manager_recovers_native_stall_and_verifies_real_tool_progress(tmp_path):
    mock = MockModel()
    a3_calls = 0
    def handler(body):
        nonlocal a3_calls
        mock.delay = 60 if body["model"] == "A2-dsv41f" and len(mock.requests) == 2 else 0
        if body["model"] == "A3-dsv41f":
            a3_calls += 1
            if a3_calls == 1:
                return function_call("exec_command", {"cmd": "printf 'RECOVERY-TOOL-OK\\n'", "workdir": str(tmp_path)})
        return None
    mock.handler = handler
    clock = [100.0]
    manager = Manager(ServiceConfig(), Store(":memory:"), clock=lambda: clock[0])
    for name, purpose in [("A3-dsv41f", "execution"), ("ZL-DeepSeek-v4.1", "recovery")]:
        manager.save("route", ModelRoute(id=name, name=name, purpose=purpose, opaque=purpose == "recovery", backend_ids=["a3"] if purpose == "execution" else []))
    for backend in ["a2", "a3"]:
        manager.save("backend", Backend(id=backend, name=backend, health="available", max_sessions=1))
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        client = consumer = None
        try:
            client = await CodexClient.unix(str(sock), codex_binary())
            thread = (await client.call("thread/start", {"cwd": str(tmp_path), "modelProvider": "mock",
                "model": "A2-dsv41f", "approvalPolicy": "never", "sandbox": "workspace-write"}))["thread"]
            sid = "h:" + thread["id"]
            manager.clients["h"] = client
            manager.create_task(Task(id="A", title="实际停滞恢复", cwd=str(tmp_path), allowed_roots=[str(tmp_path)], budget=Budget(max_requests=10)))
            manager.save("session", Session(id=sid, host_id="h", thread_id=thread["id"], task_id="A", cwd=str(tmp_path),
                model="A2-dsv41f", connected=True, control_enabled=True, last_progress_at=100,
                stall_policy=StallPolicy(enabled=True, suspect_after=900, confirm_after=1800)))
            assert manager.store.reserve_group("old", "A", {"a2": 1}, 1)
            manager.store.attach_allocation("old", sid)
            consumer = asyncio.create_task(manager._consume("h", client))
            cursor = manager.store.cursor()
            await manager.command(sid, "message", {"text": "创建初始历史 ORBIT-42"}, "first")
            await manager.wait_codex_event(sid, cursor, lambda e:e["method"] == "turn/completed", timeout=15)
            await manager.command(sid, "message", {"text": "模拟长期后端停滞"}, "stalled")
            async with asyncio.timeout(10):
                while len(mock.requests) < 2 or not manager.session(sid).active_turn_id:  # noqa: ASYNC110
                    await asyncio.sleep(.02)
            clock[0] = 5000
            manager.diagnose(sid, "model_backend", True, "隔离模型请求确认长期未输出")
            recovery = await asyncio.wait_for(manager.recover(sid, "A3-dsv41f"), 20)
            async with asyncio.timeout(15):
                while manager.store.require("recovery", recovery["id"])["state"] != "complete":  # noqa: ASYNC110
                    await asyncio.sleep(.02)
            records = manager.store.events(0, 1000, sid)
            evidence = [e for e in records if e["kind"] == "codex.event" and e["body"]["method"] == "item/completed" and e["body"].get("params", {}).get("item", {}).get("type") == "commandExecution"]
            assert evidence and evidence[-1]["body"]["params"]["item"]["exitCode"] == 0
            assert "RECOVERY-TOOL-OK" in evidence[-1]["body"]["params"]["item"]["aggregatedOutput"]
            sequence = [body["model"] for body in mock.requests]
            assert sequence[:2] == ["A2-dsv41f"] * 2 and sequence[2] == "ZL-DeepSeek-v4.1"
            assert sequence[-1] == "A3-dsv41f"
            assert manager.session(sid).control_revision == 0, json.dumps([e["body"] for e in records if e["kind"] == "codex.event" and e["body"].get("params", {}).get("item", {}).get("type") == "userMessage"], ensure_ascii=False)
            assert [row["backend_id"] for row in manager.store.allocations()] == ["a3"]
            assert manager.session(sid).backend_id == "a3"
            report = Path(".runtime/reports/manager-recovery.json")
            await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(report.write_text, json.dumps({"passed": True, "simulation": True, "native_codex": True,
                "request_sequence": sequence, "recovery_state": "complete", "tool_exit_code": 0}, ensure_ascii=False, indent=2))
        finally:
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            if client:
                await client.close()
            await stop_server(process)
            manager.store.close()


async def test_native_codex_supervisor_decides_and_repairs_original_task(tmp_path):
    mock = MockModel()
    decision = {"action": "repair", "reason": "原任务内重试", "message": "修复原目录内失败并重新验证。"}
    def handler(body):
        if body["model"] == "supervisor":
            mock.text = json.dumps(decision, ensure_ascii=False)
        else:
            mock.text = "原任务接收修复指令。"
    mock.handler = handler
    config = ServiceConfig(supervisor={"kind": "codex", "host_id": "h", "model": "supervisor",
        "model_provider": "mock", "cwd": str(tmp_path), "max_invocations": 1})
    manager = Manager(config, Store(":memory:"), clock=lambda: 5000)
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        client = consumer = None
        try:
            client = await CodexClient.unix(str(sock), codex_binary())
            thread = (await client.call("thread/start", {"cwd": str(tmp_path), "modelProvider": "mock", "model": "A2-dsv41f"}))["thread"]
            manager.clients["h"] = client
            manager.save("host", Host(id="h", name="隔离测试", address="localhost", allowed_roots=[str(tmp_path)]))
            manager.create_task(Task(id="A", title="原工作任务", cwd=str(tmp_path), allowed_roots=[str(tmp_path)]))
            session = Session(id="h:" + thread["id"], host_id="h", thread_id=thread["id"], task_id="A", cwd=str(tmp_path), connected=True, control_enabled=True)
            manager.save("session", session)
            consumer = asyncio.create_task(manager._consume("h", client))
            queue_decision(manager, session, "隔离注入的执行错误", "error", 0)
            job = manager.store.list("decision")[0]
            result = await asyncio.wait_for(Supervisor(manager).process(job["id"]), 20)
            assert result["state"] == "complete", json.dumps(result, ensure_ascii=False)
            async with asyncio.timeout(10):
                while len(mock.requests) < 2:  # noqa: ASYNC110
                    await asyncio.sleep(.02)
            assert [body["model"] for body in mock.requests] == ["supervisor", "A2-dsv41f"]
            assert decision["message"] in json.dumps(mock.requests[1]["input"], ensure_ascii=False)
            assert manager.task("A").retries_used == 1
            assert manager.store.require("supervisor", "codex")["invocations"] == 1
        finally:
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            if client:
                await client.close()
            await stop_server(process)
            manager.store.close()


@pytest.mark.parametrize("failed_model", ["ZL-DeepSeek-v4.1", "A3-dsv41f"])
async def test_native_recovery_failure_keeps_record_and_does_not_repeat_compaction(tmp_path, failed_model):
    mock = MockModel()
    clock = [100.0]
    manager = Manager(ServiceConfig(), Store(":memory:"), clock=lambda: clock[0])
    for name, purpose in [("A3-dsv41f", "execution"), ("ZL-DeepSeek-v4.1", "recovery")]:
        manager.save("route", ModelRoute(id=name, name=name, purpose=purpose, opaque=True))
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        client = consumer = None
        try:
            client = await CodexClient.unix(str(sock), codex_binary())
            thread = (await client.call("thread/start", {"cwd": str(tmp_path), "modelProvider": "mock", "model": "A2-dsv41f",
                "config": {"model_providers.mock.request_max_retries": 0, "model_providers.mock.stream_max_retries": 0}}))["thread"]
            sid = "h:" + thread["id"]
            manager.clients["h"] = client
            manager.create_task(Task(id="A", title="恢复失败记录", cwd=str(tmp_path), allowed_roots=[str(tmp_path)]))
            manager.save("session", Session(id=sid, host_id="h", thread_id=thread["id"], task_id="A", cwd=str(tmp_path), model="A2-dsv41f", connected=True, control_enabled=True, last_progress_at=100,
                stall_policy=StallPolicy(enabled=True, suspect_after=900, confirm_after=1800)))
            consumer = asyncio.create_task(manager._consume("h", client))
            cursor = manager.store.cursor()
            await manager.command(sid, "message", {"text": "创建用于恢复的历史"}, "initial")
            await manager.wait_codex_event(sid, cursor, lambda e:e["method"] == "turn/completed", timeout=15)
            clock[0] = 5000
            session = manager.session(sid)
            session.status = "active"  # 用模拟时钟与状态注入确认停滞，不干扰真实后端。
            manager.save("session", session)
            manager.diagnose(sid, "model_backend", True, "隔离停滞状态注入")
            mock.fail_models.add(failed_model)
            record = await asyncio.wait_for(manager.recover(sid, "A3-dsv41f"), 15)
            async with asyncio.timeout(10):
                while manager.store.require("recovery", record["id"])["state"] != "blocked":  # noqa: ASYNC110
                    await asyncio.sleep(.02)
            assert manager.store.require("recovery", record["id"])["error"]
            assert sum(body["model"] == "ZL-DeepSeek-v4.1" for body in mock.requests) == 1
            assert manager.task("A").retries_used == 1
            if failed_model == "ZL-DeepSeek-v4.1":
                assert all(body["model"] != "A3-dsv41f" for body in mock.requests)
                async with asyncio.timeout(3):
                    while manager.session(sid).model != "A2-dsv41f":  # noqa: ASYNC110
                        await asyncio.sleep(.02)
                assert manager.session(sid).model == "A2-dsv41f"
        finally:
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            if client:
                await client.close()
            await stop_server(process)
            manager.store.close()
