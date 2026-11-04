from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from super_brain.rpc import CodexClient
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel, function_call

pytestmark = pytest.mark.integration


async def test_killed_service_process_restarts_and_continues_completed_compaction(tmp_path):
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 localhost 测试主机密钥")
    mock = MockModel()
    compact_started, release_compact = asyncio.Event(), asyncio.Event()
    hold_a2 = False
    a3_calls = 0
    async def handler(body):
        nonlocal a3_calls
        mock.delay = 60 if hold_a2 and body["model"] == "A2-dsv41f" else 0
        if body["model"] == "ZL-DeepSeek-v4.1":
            compact_started.set()
            await release_compact.wait()
        if body["model"] == "A3-dsv41f":
            a3_calls += 1
            if a3_calls == 1:
                return function_call("exec_command", {"cmd": "printf 'SERVICE-RESTART-OK\\n'", "workdir": str(tmp_path)})
    mock.handler = handler
    async with mock.serve() as model_url:
        daemon, sock = await start_server(tmp_path, model_url)
        owner = service = None
        log = (tmp_path / "service.log").open("wb")
        try:
            owner = await CodexClient.unix(str(sock), codex_binary())
            thread = (await owner.call("thread/start", {"cwd": str(tmp_path), "modelProvider": "mock", "model": "A2-dsv41f",
                "approvalPolicy": "never", "sandbox": "workspace-write"}))["thread"]
            await owner.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "初始化进程崩溃测试历史"}]})
            await owner.wait_event("turn/completed", thread_id=thread["id"])
            owner.history.clear()
            port_probe = socket.socket()
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
            port_probe.close()
            config = {"database": str(tmp_path / "service.db"), "port": port, "poll_interval": 1, "reconnect_interval": 1,
                "token_env": "SUPER_BRAIN_ISOLATED_RESTART_TOKEN",
                "hosts": [{"id": "h", "name": "实际进程恢复测试端", "address": "localhost", "control_socket": str(sock),
                    "known_hosts_file": str(known), "codex_command": [codex_binary()], "allowed_roots": [str(tmp_path)]}],
                "backends": [{"id": "a3", "name": "模拟目标后端", "health": "available"}],
                "routes": [{"id": "a3", "name": "A3-dsv41f", "backend_ids": ["a3"]},
                    {"id": "zl", "name": "ZL-DeepSeek-v4.1", "purpose": "recovery", "opaque": True}]}
            config_file = tmp_path / "service.yaml"
            await asyncio.to_thread(config_file.write_text, yaml.safe_dump(config, allow_unicode=True))
            async def launch():
                return await asyncio.create_subprocess_exec(sys.executable, "-m", "super_brain.cli", "serve", "--config", str(config_file), stdout=log, stderr=log)
            service = await launch()
            first_pid = service.pid
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as http:
                async def wait_snapshot():
                    async with asyncio.timeout(20):
                        while True:
                            assert service.returncode is None, (tmp_path / "service.log").read_text(errors="replace")[-4000:]
                            try:
                                response = await http.get("/api/snapshot")
                                if response.is_success and response.json()["sessions"] and response.json()["sessions"][0]["connected"]:
                                    return response.json()
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(.05)
                state = await wait_snapshot()
                sid = state["sessions"][0]["id"]
                response = await http.post("/api/tasks", json={"id": "A", "title": "实际服务进程重启恢复", "cwd": str(tmp_path), "allowed_roots": [str(tmp_path)],
                    "budget": {"max_requests": 20, "max_retries": 2}})
                assert response.is_success, response.text
                response = await http.post(f"/api/sessions/{sid}/bind", json={"task_id": "A", "control_enabled": True})
                assert response.is_success, response.text
                hold_a2 = True
                response = await http.post(f"/api/sessions/{sid}/command", json={"action": "message", "payload": {"text": "隔离测试中的停滞请求"}, "operation_id": "start-stall"})
                assert response.is_success, response.text
                response = await http.post(f"/api/sessions/{sid}/policy", json={"enabled": True, "suspect_after": 1, "confirm_after": 2, "cooldown": 60})
                assert response.is_success
                response = await http.post("/api/manager-action", json={"tool": "session_diagnose", "arguments": {
                    "session_id": sid, "cause": "model_backend", "confirmed": True, "reason": "专用模拟请求确认阻塞，验证进程退出恢复"}})
                assert response.is_success, response.text
                await asyncio.wait_for(compact_started.wait(), 15)
                async with asyncio.timeout(10):
                    while True:
                        state = (await http.get("/api/snapshot")).json()
                        records = state["recoveries"]
                        if records and records[0].get("compaction_turn_id"):
                            record = records[0]
                            break
                        await asyncio.sleep(.03)
                service.kill()
                await service.wait()
                assert service.returncode != 0
                release_compact.set()
                await owner.wait_for(lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == record["compaction_turn_id"], timeout=15)
                service = await launch()
                assert service.pid != first_pid
                await wait_snapshot()
                async with asyncio.timeout(20):
                    while True:
                        state = (await http.get("/api/snapshot")).json()
                        result = next(item for item in state["recoveries"] if item["id"] == record["id"])
                        if result["state"] == "complete":
                            break
                        await asyncio.sleep(.05)
                await owner.wait_for(lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == result["continuation_turn_id"], timeout=15)
                assert sum(body["model"] == "ZL-DeepSeek-v4.1" for body in mock.requests) == 1
                assert a3_calls == 2 and state["tasks"][0]["retries_used"] == 1
                report = Path(".runtime/reports/service-process-restart.json")
                await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(report.write_text, json.dumps({"passed": True, "first_pid": first_pid, "second_pid": service.pid,
                    "native_codex": True, "real_models": False, "hard_process_kill": True, "recovery_state": "complete",
                    "request_sequence": [body["model"] for body in mock.requests]}, ensure_ascii=False, indent=2))
        finally:
            release_compact.set()
            if service and service.returncode is None:
                service.terminate()
                try:
                    await asyncio.wait_for(service.wait(), 10)
                except TimeoutError:
                    service.kill()
                    await service.wait()
            log.close()
            if owner:
                await owner.close()
            await stop_server(daemon)
