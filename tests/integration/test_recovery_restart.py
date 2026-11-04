from __future__ import annotations

import asyncio
import json
import time
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
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel, function_call

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("fault", ["during_compaction", "lost_continuation_reply", "manual_during_disconnect"])
async def test_native_recovery_restart_uses_history_and_never_repeats_work(tmp_path, fault):
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 localhost 测试主机密钥")
    mock = MockModel()
    calls_a3 = 0
    def handler(body):
        nonlocal calls_a3
        mock.delay = .4 if body["model"] == "ZL-DeepSeek-v4.1" else 0
        if body["model"] == "A3-dsv41f":
            calls_a3 += 1
            if calls_a3 == 1:
                return function_call("exec_command", {"cmd": "printf 'RESTART-PROGRESS-42\\n'", "workdir": str(tmp_path)})
    mock.handler = handler
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        config = ServiceConfig(database=str(tmp_path / "state.db"), poll_interval=1, reconnect_interval=1,
            hosts=[Host(id="h", name="恢复重启验收", address="localhost", enabled=False, control_socket=str(sock),
                codex_command=[codex_binary()], known_hosts_file=str(known), allowed_roots=[str(tmp_path)])],
            backends=[Backend(id="a2", name="a2", health="available"), Backend(id="a3", name="a3", health="available")],
            routes=[ModelRoute(id="a3", name="A3-dsv41f", backend_ids=["a3"]),
                ModelRoute(id="zl", name="ZL-DeepSeek-v4.1", purpose="recovery", opaque=True)])
        first = Manager(config)
        owner = observer = consumer = work = second = None
        try:
            owner = await CodexClient.unix(str(sock), codex_binary())
            thread = (await owner.call("thread/start", {"cwd": str(tmp_path), "modelProvider": "mock", "model": "A2-dsv41f",
                "approvalPolicy": "never", "sandbox": "workspace-write"}))["thread"]
            await owner.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "创建重启验收历史"}]})
            await owner.wait_event("turn/completed", thread_id=thread["id"])
            owner.history.clear()
            observer = await CodexClient.unix(str(sock), codex_binary())
            await observer.call("thread/resume", {"threadId": thread["id"], "excludeTurns": True})
            first.clients["h"] = observer
            first.create_task(Task(id="A", title="恢复中断后推进原任务", cwd=str(tmp_path), allowed_roots=[str(tmp_path)],
                budget=Budget(max_requests=10, max_retries=2)))
            sid = "h:" + thread["id"]
            first.save("session", Session(id=sid, host_id="h", thread_id=thread["id"], task_id="A", cwd=str(tmp_path),
                model="A2-dsv41f", connected=True, control_enabled=True, status="active", last_progress_at=time.time()-4000,
                stall_policy=StallPolicy(enabled=True, suspect_after=900, confirm_after=1800)))
            first.diagnose(sid, "model_backend", True, "隔离状态注入，验证中断恢复")
            assert first.store.reserve_group("original", "A", {"a2": 1}, 1)
            first.store.attach_allocation("original", sid)
            consumer = asyncio.create_task(first._consume("h", observer))
            accepted_turn = None
            if fault == "lost_continuation_reply":
                class LoseReply:
                    @property
                    def connected(self):
                        return observer.connected
                    async def call(self, method, params):
                        nonlocal accepted_turn
                        if method == "turn/start":
                            consumer.cancel()
                            await asyncio.gather(consumer, return_exceptions=True)
                            result = await observer.call(method, params)
                            accepted_turn = result["turn"]["id"]
                            raise ConnectionError("隔离注入：daemon 已接受接续，但管理端未收到回复")
                        return await observer.call(method, params)
                first.clients["h"] = LoseReply()
            work = asyncio.create_task(first.recover(sid, "A3-dsv41f"))
            if fault == "lost_continuation_reply":
                record = await asyncio.wait_for(work, 20)
                assert record["state"] == "reconcile_required"
                await owner.wait_for(lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == accepted_turn, timeout=20)
            else:
                async with asyncio.timeout(15):
                    while True:
                        records = first.store.list("recovery")
                        if records and records[0].get("compaction_turn_id"):
                            record = records[0]
                            break
                        await asyncio.sleep(.02)
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
                await owner.wait_for(lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == record["compaction_turn_id"], timeout=20)
            await observer.close()
            observer = None
            first.clients.clear()
            first.store.close()
            first = None
            if fault == "manual_during_disconnect":
                await owner.call("thread/settings/update", {"threadId": thread["id"], "model": "A2-dsv41f"})
                await owner.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "人工调整后的原任务，保留当前选择。"}]})
                await owner.wait_for(lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] != record["compaction_turn_id"], timeout=15)
            requests_before_restart = len(mock.requests)
            config.hosts[0].enabled = True
            second = Manager(config)
            await second.start()
            expected_state = "cancelled" if fault == "manual_during_disconnect" else "complete"
            async with asyncio.timeout(20):
                while second.store.require("recovery", record["id"])["state"] != expected_state:  # noqa: ASYNC110
                    await asyncio.sleep(.03)
            result = second.store.require("recovery", record["id"])
            assert sum(body["model"] == "ZL-DeepSeek-v4.1" for body in mock.requests) == 1
            assert second.task("A").retries_used == 1
            if fault == "during_compaction":
                await owner.wait_for(lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == result["continuation_turn_id"], timeout=15)
                assert calls_a3 == 2
                assert second.session(sid).backend_id == "a3"
                assert [row["backend_id"] for row in second.store.allocations()] == ["a3"]
            else:
                assert len(mock.requests) == requests_before_restart, "核对不应重复发送已经执行的工作"
            if fault == "manual_during_disconnect":
                assert calls_a3 == 0 and second.session(sid).model == "A2-dsv41f"
            report = Path(".runtime/reports") / ("recovery-restart-" + fault + ".json")
            await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(report.write_text, json.dumps({"passed": True, "fault": fault, "native_codex": True,
                "real_models": False, "state": result["state"], "request_sequence": [body["model"] for body in mock.requests],
                "retries_used": 1}, ensure_ascii=False, indent=2))
        finally:
            if work and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            if first:
                first.clients.clear()
                first.store.close()
            if second:
                await second.stop()
                second.store.close()
            if observer:
                await observer.close()
            if owner:
                await owner.close()
            await stop_server(process)
