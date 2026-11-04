from __future__ import annotations

import asyncio
import json
import os
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
from super_brain.rpc import CodexClient
from super_brain.store import Conflict, Store
from super_brain.worker import worker_config
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel
from tests.test_api import serve_app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("max_agents", [0, 1])
async def test_native_dispatch_uses_task_gateway_and_disables_unbudgeted_agents(monkeypatch, tmp_path, max_agents):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "isolated-admin-token")
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 SSH localhost 的测试主机密钥")
    mock = MockModel()
    async with mock.serve() as upstream:
        process, sock = None, tmp_path / "control.sock"
        config = ServiceConfig(database=":memory:", poll_interval=1,
            hosts=[Host(id="local", name="测试主机", address="localhost", control_socket=str(sock),
                        known_hosts_file=str(known), codex_command=[codex_binary()], allowed_roots=[str(tmp_path)])],
            backends=[Backend(id="pool", name="测试共享后端", health="available", max_requests=1, max_sessions=2)],
            routes=[ModelRoute(id="a", name="A2-dsv41f", backend_ids=["pool"], upstream_model="deepseek-flash")],
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"))
        manager = Manager(config, Store(":memory:"))
        app = create_app(config, manager)
        try:
            async with serve_app(app) as url:
                config.inference.public_url = url
                process, sock = await start_server(tmp_path, url + "/inference/v1",
                    config_text=worker_config(url + "/inference/v1", manager.gateway.worker_token("local"), "A2-dsv41f"))
                await asyncio.wait_for(manager.host_ready.setdefault("local", asyncio.Event()).wait(), 15)
                task = Task(id="managed", title="受管执行验证", objective="仅返回模拟进度，不调用工具。",
                    host_id="local", cwd=str(tmp_path), allowed_roots=[str(tmp_path)], allowed_models=["A2-dsv41f"],
                    budget=Budget(max_requests=1, max_agents=max_agents, max_sessions=2))
                manager.create_task(task)
                cursor = manager.store.cursor()
                dispatches = await asyncio.gather(manager.dispatch(task.id), manager.dispatch(task.id), return_exceptions=True)
                assert sum(isinstance(value, Conflict) for value in dispatches) == 1
                result = next(value for value in dispatches if isinstance(value, dict))
                await manager.wait_codex_event(result["session_id"], cursor, lambda e:e["method"] == "turn/completed")
                assert len((await manager.clients["local"].call("thread/loaded/list"))["data"]) == 1
                assert len(mock.requests) == 1
                assert mock.requests[0]["model"] == "deepseek-flash"
                assert manager.task(task.id).requests_used == 1
                assert manager.task(task.id).tokens_used == 70
                tools = mock.requests[0].get("tools", [])
                if max_agents == 0:
                    assert "spawn_agent" not in json.dumps(tools), tools
                else:
                    report = Path(__file__).parents[2] / ".runtime/reports/agent-tools.json"
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text(json.dumps(tools, ensure_ascii=False, indent=2))
                    assert "spawn_agent" in json.dumps(tools), tools
                assert manager.session(result["session_id"]).model_provider == "super_brain"
                assert manager.session(result["session_id"]).budget_accounting == "gateway"
        finally:
            manager.store.close()
            await stop_server(process)


async def test_user_native_compaction_can_use_zl_without_manager_stall_recovery(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "native-compact-token")
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 SSH localhost 的测试主机密钥")
    mock = MockModel()
    async with mock.serve() as upstream:
        process, sock = None, tmp_path / "control.sock"
        config = ServiceConfig(database=":memory:", poll_interval=1,
            hosts=[Host(id="h", name="原生压缩测试端", address="localhost", control_socket=str(sock),
                known_hosts_file=str(known), codex_command=[codex_binary()], allowed_roots=[str(tmp_path)])],
            backends=[Backend(id="pool", name="共享池", health="available")],
            routes=[ModelRoute(id=name, name=name, backend_ids=[] if purpose == "recovery" else ["pool"],
                upstream_model="deepseek-flash", purpose=purpose, opaque=purpose == "recovery")
                for name, purpose in [("A2-dsv41f", "execution"), ("A3-dsv41f", "execution"), ("ZL-DeepSeek-v4.1", "recovery")]],
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"))
        manager = Manager(config, Store(":memory:"))
        owner = None
        try:
            async with serve_app(create_app(config, manager)) as url:
                config.inference.public_url = url
                process, sock = await start_server(tmp_path, url + "/inference/v1",
                    config_text=worker_config(url + "/inference/v1", manager.gateway.worker_token("h"), "A2-dsv41f"))
                await asyncio.wait_for(manager.host_ready.setdefault("h", asyncio.Event()).wait(), 15)
                manager.create_task(Task(id="A", title="用户原生压缩", objective="仅返回模拟测试进度", host_id="h",
                    cwd=str(tmp_path), allowed_roots=[str(tmp_path)], allowed_models=["A2-dsv41f", "A3-dsv41f"],
                    budget=Budget(max_requests=4, max_tokens=100000)))
                cursor = manager.store.cursor()
                started = await manager.dispatch("A")
                sid = started["session_id"]
                thread_id = manager.session(sid).thread_id
                await manager.wait_codex_event(sid, cursor, lambda event:event["method"] == "turn/completed")
                assert not manager.session(sid).stall_policy.enabled
                owner = await CodexClient.unix(str(sock), codex_binary())
                await owner.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                await owner.call("thread/settings/update", {"threadId": thread_id, "model": "ZL-DeepSeek-v4.1"})
                await owner.call("thread/compact/start", {"threadId": thread_id})
                compact = await owner.wait_for(lambda event:event["method"] == "item/completed" and event.get("params", {}).get("item", {}).get("type") == "contextCompaction", timeout=15)
                await owner.wait_for(lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == compact["params"]["turnId"], timeout=15)
                await owner.call("thread/settings/update", {"threadId": thread_id, "model": "A3-dsv41f"})
                await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "用户压缩后继续原任务"}]})
                await owner.wait_event("turn/completed", thread_id=thread_id)
                assert len(mock.requests) == 3
                assert manager.task("A").requests_used == 3
                assert not manager.store.list("recovery"), "用户原生压缩不得伪装成管理器的停滞恢复"
                rows = manager.store.conn.execute("SELECT model FROM inference_requests ORDER BY created").fetchall()
                assert [row[0] for row in rows] == ["A2-dsv41f", "ZL-DeepSeek-v4.1", "A3-dsv41f"]
                report = Path(".runtime/reports/native-user-compaction.json")
                await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(report.write_text, json.dumps({"passed": True, "native_codex": True, "real_models": False,
                    "manager_stall_recovery_enabled": False, "manager_recoveries": 0, "requests_charged": 3,
                    "request_sequence": [row[0] for row in rows]}, ensure_ascii=False, indent=2))
        finally:
            if owner:
                await owner.close()
            manager.store.close()
            await stop_server(process)


@pytest.mark.parametrize("owner_first", [True, False])
async def test_daemon_restart_preserves_managed_request_budget(monkeypatch, tmp_path, owner_first):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "restart-managed-token")
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 SSH localhost 的测试主机密钥")
    mock = MockModel()
    async with mock.serve() as upstream:
        process, sock = None, tmp_path / "control.sock"
        config = ServiceConfig(database=":memory:", poll_interval=1, reconnect_interval=1,
            hosts=[Host(id="h", name="daemon 重启预算测试", address="localhost", control_socket=str(sock),
                known_hosts_file=str(known), codex_command=[codex_binary()], allowed_roots=[str(tmp_path)])],
            backends=[Backend(id="pool", name="共享池", health="available")],
            routes=[ModelRoute(id="a2", name="A2-dsv41f", backend_ids=["pool"], upstream_model="deepseek-flash")],
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"))
        manager = Manager(config, Store(":memory:"))
        owner = None
        try:
            async with serve_app(create_app(config, manager)) as url:
                config.inference.public_url = url
                process, sock = await start_server(tmp_path, url + "/inference/v1",
                    config_text=worker_config(url + "/inference/v1", manager.gateway.worker_token("h"), "A2-dsv41f"))
                await asyncio.wait_for(manager.host_ready.setdefault("h", asyncio.Event()).wait(), 15)
                manager.create_task(Task(id="A", title="重启后仍受同一预算约束", objective="只返回测试进度", host_id="h",
                    cwd=str(tmp_path), allowed_roots=[str(tmp_path)], budget=Budget(max_requests=2, max_tokens=100000)))
                cursor = manager.store.cursor()
                dispatched = await manager.dispatch("A")
                sid = dispatched["session_id"]
                thread_id = manager.session(sid).thread_id
                await manager.wait_codex_event(sid, cursor, lambda event:event["method"] == "turn/completed")
                assert manager.task("A").requests_used == 1 and len(mock.requests) == 1
                await stop_server(process)
                async with asyncio.timeout(15):
                    while manager.session(sid).connected:  # noqa: ASYNC110
                        await asyncio.sleep(.03)
                host_job = next(job for job in manager.jobs if job.get_coro().__name__ == "_host_loop")
                host_job.cancel()
                await asyncio.gather(host_job, return_exceptions=True)
                if sock.exists():
                    await asyncio.to_thread(sock.unlink)
                process = await asyncio.create_subprocess_exec(codex_binary(), "app-server", "--listen", "unix://" + str(sock),
                    env={**os.environ, "CODEX_HOME": str(tmp_path / "codex-home")}, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                async with asyncio.timeout(15):
                    while not sock.exists():  # noqa: ASYNC110
                        await asyncio.sleep(.03)
                owner = await CodexClient.unix(str(sock), codex_binary())
                if owner_first:
                    await owner.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                    await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "尚未核对预算身份时拒绝推理"}]})
                    rejected = await owner.wait_event("turn/completed", thread_id=thread_id)
                    assert rejected["params"]["turn"]["status"] == "failed"
                    assert len(mock.requests) == 1 and manager.task("A").requests_used == 1
                manager.jobs.append(asyncio.create_task(manager._host_loop("h")))
                async with asyncio.timeout(15):
                    while not manager.session(sid).connected or not manager.session(sid).budget_ready:  # noqa: ASYNC110
                        await asyncio.sleep(.03)
                if not owner_first:
                    assert any(event["kind"] == "budget.worker_restored" for event in manager.store.events(0, 1000, sid))
                    await owner.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "重启后第二次请求"}]})
                await owner.wait_event("turn/completed", thread_id=thread_id)
                assert len(mock.requests) == 2
                assert manager.task("A").requests_used == 2, "daemon 重启后请求绕过了任务网关"
                await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "预算耗尽时不得调用上游"}]})
                await owner.wait_event("turn/completed", thread_id=thread_id)
                assert len(mock.requests) == 2
                report = Path(f".runtime/reports/daemon-restart-budget-{'owner' if owner_first else 'manager'}.json")
                await asyncio.to_thread(report.write_text, json.dumps({"passed": True,
                    "native_codex": True, "real_models": False, "owner_resumed_first": owner_first,
                    "task_requests_charged": manager.task("A").requests_used, "upstream_requests": len(mock.requests),
                    "exhausted_budget_refused": True, "same_thread": manager.session(sid).thread_id == thread_id}, ensure_ascii=False, indent=2))
        finally:
            if owner:
                await owner.close()
            manager.store.close()
            await stop_server(process)
