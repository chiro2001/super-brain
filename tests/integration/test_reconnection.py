from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from super_brain.manager import Manager
from super_brain.models import Host, ServiceConfig, Task
from super_brain.probe import check_remote_scope
from super_brain.rpc import CodexClient
from super_brain.store import Store
from super_brain.supervisor import Supervisor, queue_decision
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel, function_call

pytestmark = pytest.mark.integration


async def test_ssh_disconnect_reconciles_completed_output_without_resending(tmp_path):
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 localhost 测试主机密钥")
    mock = MockModel()
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        owner = None
        manager = Manager(ServiceConfig(poll_interval=1, reconnect_interval=1,
            hosts=[Host(id="h", name="重连测试", address="localhost", control_socket=str(sock),
                known_hosts_file=str(known), codex_command=[codex_binary()], allowed_roots=[str(tmp_path)])]), Store(":memory:"))
        hidden = sock.with_suffix(".hidden")
        try:
            owner = await CodexClient.unix(str(sock), codex_binary())
            thread = (await owner.call("thread/start", {"cwd": str(tmp_path), "modelProvider": "mock", "model": "A2-dsv41f",
                "approvalPolicy": "never", "sandbox": "workspace-write"}))["thread"]
            await owner.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "初始历史"}]})
            await owner.wait_event("turn/completed", thread_id=thread["id"])
            await manager.start()
            sid = "h:" + thread["id"]
            async with asyncio.timeout(15):
                while not manager.store.get("session", sid) or not manager.session(sid).connected or manager.session(sid).cwd != str(tmp_path) or not any(  # noqa: ASYNC110
                    event["kind"] == "observation.history_reconciled" for event in manager.store.events(0, 1000, sid)
                ):
                    await asyncio.sleep(.03)
            manager.create_task(Task(id="work", title="监听原任务", host_id="h", cwd=str(tmp_path), allowed_roots=[str(tmp_path)], acceptance="核对实际工具结果"))
            manager.bind_session(sid, "work", True)
            old_control = manager.session(sid).control_revision
            queue_decision(manager, manager.session(sid), "掉线前的旧判断", "error", 0)
            old_decision = manager.store.list("decision")[0]
            # 仅移走测试 socket 路径，现有 owner 连接仍可工作，重连暂时失败。
            await asyncio.to_thread(sock.rename, hidden)
            transport = manager.clients["h"]
            transport.process.terminate()
            await transport.process.wait()
            async with asyncio.timeout(10):
                while manager.session(sid).connected:  # noqa: ASYNC110
                    await asyncio.sleep(.03)
            assert manager.assess(manager.session(sid))["state"] == "observation_lost"
            mock.text = "OFFLINE-HISTORY-42"
            offline_calls = 0
            def handler(body):
                nonlocal offline_calls
                offline_calls += 1
                if offline_calls == 1:
                    return function_call("exec_command", {"cmd": "printf 'OFFLINE-TOOL-OK\\n'", "workdir": str(tmp_path)})
            mock.handler = handler
            await owner.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "在观测断线时完成"}]})
            await owner.wait_event("turn/completed", thread_id=thread["id"])
            await asyncio.to_thread(hidden.rename, sock)
            async with asyncio.timeout(15):
                while not manager.session(sid).connected or not any(  # noqa: ASYNC110
                    event["kind"] == "codex.event" and event["body"].get("historyRecovered")
                    and event["body"].get("params", {}).get("item", {}).get("text") == "OFFLINE-HISTORY-42"
                    for event in manager.store.events(0, 1000, sid)
                ):
                    await asyncio.sleep(.03)
            events = manager.store.events(0, 1000, sid)
            recovered = [event for event in events if event["kind"] == "codex.event" and event["body"].get("historyRecovered") and event["body"].get("params", {}).get("item", {}).get("text") == "OFFLINE-HISTORY-42"]
            assert len(recovered) == 1
            assert len(mock.requests) == 3, "重连不应触发额外模型请求"
            assert manager.session(sid).control_revision == old_control + 1
            assert manager.session(sid).progress_revision == 1
            assert (await Supervisor(manager).process(old_decision["id"]))["state"] == "resolved"
            assert any(job["source"] == "verify" for job in manager.store.list("decision"))
            before_overflow = manager.store.cursor()
            transport = manager.clients["h"]
            for index in range(4200):
                transport._dispatch({"method": "item/agentMessage/delta", "params": {
                    "threadId": thread["id"], "itemId": "overflow", "delta": str(index)}})
            assert transport.resync_required
            async with asyncio.timeout(15):
                while manager.clients.get("h") is transport or not manager.session(sid).connected or not any(  # noqa: ASYNC110
                    event["kind"] == "observation.history_reconciled" for event in manager.store.events(before_overflow, 1000)
                ):
                    await asyncio.sleep(.03)
            events = manager.store.events(before_overflow, 1000)
            assert any(event["kind"] == "observation.resync_required" for event in events)
            assert any(event["kind"] == "observation.history_reconciled" for event in events)
            assert len(mock.requests) == 3
            assert manager.session(sid).control_revision == old_control + 1
        finally:
            await manager.stop()
            manager.store.close()
            if owner:
                await owner.close()
            await stop_server(process)


async def test_real_ssh_quotes_unicode_paths_and_rejects_symlink_escape(tmp_path):
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 localhost 测试主机密钥")
    root = tmp_path / "中文 项目 ' $() ;"
    outside = tmp_path / "outside"
    await asyncio.to_thread(root.mkdir)
    await asyncio.to_thread(outside.mkdir)
    await asyncio.to_thread((root / "escape").symlink_to, outside, target_is_directory=True)
    host = Host(name="目录引用测试", address="localhost", known_hosts_file=str(known))
    result = await check_remote_scope(host, str(root), [str(root)])
    assert result["allowed"] and result["cwd"] == str(root)
    escaped = await check_remote_scope(host, str(root / "escape"), [str(root)])
    assert not escaped["allowed"]
