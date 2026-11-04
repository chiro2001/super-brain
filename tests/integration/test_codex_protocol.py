from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from super_brain.manager import Manager
from super_brain.models import Host, ServiceConfig
from super_brain.probe import probe_host
from super_brain.rpc import CodexClient
from super_brain.store import Store
from tests.support.mock_model import MockModel

pytestmark = pytest.mark.integration


def codex_binary() -> str:
    selected = os.environ.get("SUPER_BRAIN_TEST_CODEX_BINARY")
    if selected:
        return selected
    # 本机启动适配的二进制与 wrapper 同目录，不使用或修改用户真实配置。
    entry = shutil.which("codex")
    if not entry:
        pytest.skip("未安装 Codex")
    sibling = Path(entry).resolve().parent / "bin" / "codex"
    return str(sibling) if sibling.is_file() else entry


async def start_server(tmp_path: Path, base_url: str, *, config_text: str | None = None):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(config_text or (
        'model_provider = "mock"\nmodel = "A2-dsv41f"\n'
        'check_for_update_on_startup = false\n'
        '[model_providers.mock]\nname = "隔离测试"\n'
        f'base_url = "{base_url}"\nwire_api = "responses"\n'
        'requires_openai_auth = false\n'
        '[analytics]\nenabled = false\n'), encoding="utf-8",
    )
    sock = tmp_path / "control.sock"
    env = {**os.environ, "CODEX_HOME": str(home)}
    process = await asyncio.create_subprocess_exec(
        codex_binary(), "app-server", "--listen", f"unix://{sock}",
        env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    async with asyncio.timeout(15):
        while not sock.exists():
            if process.returncode is not None:
                raise RuntimeError((await process.stderr.read()).decode(errors="replace"))
            await asyncio.sleep(0.05)
    return process, sock


async def stop_server(process):
    if process is None:
        return
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


async def test_existing_session_goal_compact_and_continue(tmp_path):
    mock = MockModel()
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        owner = manager = None
        try:
            owner = await CodexClient.unix(str(sock), codex_binary())
            result = await owner.call("thread/start", {"model": "A2-dsv41f", "modelProvider": "mock",
                "cwd": str(tmp_path), "approvalPolicy": "never", "sandbox": "workspace-write"})
            thread_id = result["thread"]["id"]
            await owner.call("thread/goal/set", {"threadId": thread_id, "objective": "保留 ORBIT-42 并完成隔离测试",
                "origin": "user", "status": "paused"})
            mock.delay = 0.5
            await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "任务事实 ORBIT-42，请返回进度。"}]})
            # 第二客户端在 turn 已启动后接入，验证已有活动会话，而非替代会话。
            manager = await CodexClient.unix(str(sock), codex_binary())
            loaded = await manager.call("thread/loaded/list")
            assert thread_id in str(loaded)
            resumed = await manager.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            assert resumed["thread"]["id"] == thread_id
            await owner.wait_event("turn/completed", thread_id=thread_id)
            goal = await manager.call("thread/goal/get", {"threadId": thread_id})
            assert "ORBIT-42" in goal["goal"]["objective"]
            await manager.call("thread/settings/update", {"threadId": thread_id, "model": "ZL-DeepSeek-v4.1"})
            await manager.call("thread/compact/start", {"threadId": thread_id})
            await manager.wait_for(lambda event: event.get("params", {}).get("threadId") == thread_id and (
                event["method"] == "thread/compacted" or (
                    event["method"] == "item/completed" and event["params"].get("item", {}).get("type") == "contextCompaction"
                )))
            await manager.wait_event("turn/completed", thread_id=thread_id)
            await manager.call("thread/settings/update", {"threadId": thread_id, "model": "A3-dsv41f"})
            await manager.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "继续原任务，返回保留的关键事实。"}]})
            await manager.wait_event("turn/completed", thread_id=thread_id)
            models = [request.get("model") for request in mock.requests]
            assert models[0] == "A2-dsv41f", models
            assert "ZL-DeepSeek-v4.1" in models, models
            assert models[-1] == "A3-dsv41f", models
        except Exception:
            if manager:
                print("事件末尾：", list(manager.history)[-12:])
                print("proxy 错误：", list(manager.stderr))
            print("模型请求：", [(r.get("path"),r.get("model")) for r in mock.requests[-20:]])
            raise
        finally:
            if manager:
                await manager.close()
            if owner:
                await owner.close()
            await stop_server(process)


@pytest.mark.parametrize("transport", ["stdio", "tunnel"])
async def test_ssh_localhost_can_observe_existing_session(tmp_path, transport):
    """SSH-01/CODEX-01：经实际 SSH 接入已有会话。"""
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("尚未准备专用 localhost known_hosts")
    mock = MockModel()
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        owner = remote = None
        try:
            owner = await CodexClient.unix(str(sock), codex_binary())
            result = await owner.call("thread/start", {"cwd": str(tmp_path), "model": "A2-dsv41f", "modelProvider": "mock"})
            thread_id = result["thread"]["id"]
            await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "创建可接续的隔离测试历史。"}]})
            await owner.wait_event("turn/completed", thread_id=thread_id)
            remote = await CodexClient.ssh(Host(name="localhost", address="localhost", control_socket=str(sock),
                codex_command=[codex_binary()], known_hosts_file=str(known), transport=transport))
            result = await remote.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            assert result["thread"]["id"] == thread_id
            assert len(mock.requests) == 1
            discovered = await probe_host(Host(name="localhost", address="localhost", known_hosts_file=str(known)))
            assert any(endpoint["socket"] == str(sock) for endpoint in discovered["endpoints"])
        finally:
            if remote:
                await remote.close()
            if owner:
                await owner.close()
            await stop_server(process)


async def test_native_auto_compaction_is_preserved_without_manager_recovery(tmp_path):
    """STAll-06：只降低隔离会话阈值，原生自动压缩仍用执行入口。"""
    mock = MockModel()
    def handler(body):
        mock.input_tokens = 60000 if len(mock.requests) == 1 else 50
    mock.handler = handler
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        client = None
        try:
            client = await CodexClient.unix(str(sock), codex_binary())
            thread = (await client.call("thread/start", {"cwd": str(tmp_path), "model": "A2-dsv41f", "modelProvider": "mock",
                "config": {"model_auto_compact_token_limit": 50000}}))["thread"]
            await client.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "初始测试历史"}]})
            await client.wait_event("turn/completed", thread_id=thread["id"])
            client.history.clear()
            await client.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "继续下一阶段，保留 ORBIT-42。"}]})
            await client.wait_for(lambda e:e["method"] == "item/completed" and e.get("params", {}).get("item", {}).get("type") == "contextCompaction", timeout=15)
            await client.wait_event("turn/completed", thread_id=thread["id"], timeout=15)
            assert len(mock.requests) >= 3
            assert all(body["model"] == "A2-dsv41f" for body in mock.requests)
        finally:
            if client:
                await client.close()
            await stop_server(process)


async def test_manager_attaches_to_active_turn_without_relying_on_past_events(tmp_path):
    mock = MockModel()
    mock.delay = 20
    async with mock.serve() as url:
        process, sock = await start_server(tmp_path, url)
        owner = observer = None
        manager = Manager(ServiceConfig(), Store(":memory:"))
        try:
            owner = await CodexClient.unix(str(sock), codex_binary())
            thread = (await owner.call("thread/start", {"cwd": str(tmp_path), "model": "A2-dsv41f", "modelProvider": "mock"}))["thread"]
            started = await owner.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "保持活动以验证接入"}]})
            async with asyncio.timeout(10):
                while not mock.requests:  # noqa: ASYNC110
                    await asyncio.sleep(.02)
            observer = await CodexClient.unix(str(sock), codex_binary())
            manager.clients["h"] = observer
            # 不启动事件消费者，必须从权威历史获得接入前已开始的回合。
            await manager.sync_host("h", observer, set())
            sid = "h:" + thread["id"]
            assert manager.session(sid).active_turn_id == started["turn"]["id"]
            await manager.command(sid, "interrupt", {"turn_id": started["turn"]["id"]}, "stop-existing", actor="user")
            event = await owner.wait_event("turn/completed", thread_id=thread["id"])
            assert event["params"]["turn"]["status"] == "interrupted"
        finally:
            manager.store.close()
            if observer:
                await observer.close()
            if owner:
                await owner.close()
            await stop_server(process)
