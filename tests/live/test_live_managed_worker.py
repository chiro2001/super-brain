from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from super_brain.api import create_app
from super_brain.config import load_config
from super_brain.credentials import from_litellm
from super_brain.manager import Manager
from super_brain.models import Budget, Host, Task
from super_brain.rpc import CodexClient
from super_brain.store import Store
from super_brain.usage import UsageLedger
from super_brain.worker import worker_config
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.test_api import serve_app

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SUPER_BRAIN_LIVE") != "1", reason="仅在明确启用真实联调时运行")]


async def test_real_flash_managed_worker_restart_and_budget(monkeypatch, tmp_path):
    root = Path(__file__).parents[2]
    known = root / ".runtime/test-ssh/known_hosts"
    assert known.is_file(), "首版真实验收需要实际 SSH localhost"
    credentials = from_litellm(Path(os.environ.get("SUPER_BRAIN_LITELLM_DIR", "/home/chiro/litellm")))
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "isolated-live-worker-admin")
    if credentials.gateway_key:
        monkeypatch.setenv("SUPER_BRAIN_UPSTREAM_KEY", credentials.gateway_key)
    else:
        monkeypatch.delenv("SUPER_BRAIN_UPSTREAM_KEY", raising=False)
    ledger_path = str(root / ".runtime/live-usage.db")
    ledger = UsageLedger(ledger_path)
    before = ledger.summary()
    ledger.close()
    config = load_config(str(root / "config.deepseek-flash.example.yaml"))
    config.database = ":memory:"
    config.poll_interval = config.reconnect_interval = 1
    config.inference.upstream_url = credentials.gateway_url
    config.inference.ledger_path = ledger_path
    config.inference.max_output_tokens = 1024
    config.hosts = [Host(id="h", name="真实 deepseek-flash 隔离执行端", address="localhost",
        control_socket=str(tmp_path / "control.sock"), known_hosts_file=str(known),
        codex_command=[codex_binary()], allowed_roots=[str(tmp_path)])]
    manager = Manager(config, Store(":memory:"))
    process = owner = None
    result = {"passed": False, "real_models": True, "upstream_model": "deepseek-flash",
        "actual_ssh": True, "production_zl_called": False}
    try:
        async with serve_app(create_app(config, manager)) as url:
            config.inference.public_url = url
            process, sock = await start_server(tmp_path, url + "/inference/v1",
                config_text=worker_config(url + "/inference/v1", manager.gateway.worker_token("h"), "deepseek-flash"))
            await asyncio.wait_for(manager.host_ready.setdefault("h", asyncio.Event()).wait(), 20)
            manager.create_task(Task(id="A", title="首版真实受管 worker 验收",
                objective="这是隔离验证任务，不要执行任何工具。第一阶段严格只回复 FIRST-FLASH-42。之后收到第二阶段指令时严格只回复 SECOND-FLASH-42。",
                host_id="h", cwd=str(tmp_path), allowed_roots=[str(tmp_path)], allowed_models=["deepseek-flash"],
                budget=Budget(max_requests=2, max_tokens=150000)))
            cursor = manager.store.cursor()
            dispatched = await manager.dispatch("A")
            assert "session_id" in dispatched, dispatched
            sid = dispatched["session_id"]
            thread_id = manager.session(sid).thread_id
            first = await manager.wait_codex_event(sid, cursor, lambda event: event["method"] == "turn/completed", timeout=180)
            assert first["params"]["turn"]["status"] == "completed", first
            first_text = "".join(event["body"].get("params", {}).get("delta", "")
                for event in manager.store.events(cursor, 1000, sid) if event["body"].get("method") == "item/agentMessage/delta")
            assert "FIRST-FLASH-42" in first_text
            assert manager.task("A").requests_used == 1
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
                env={**os.environ, "CODEX_HOME": str(tmp_path / "codex-home")},
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            async with asyncio.timeout(15):
                while not sock.exists():  # noqa: ASYNC110
                    await asyncio.sleep(.03)
            owner = await CodexClient.unix(str(sock), codex_binary())
            # 原生客户端先恢复，确保覆盖已丢失时也经默认 worker 身份继续计账。
            await owner.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            manager.jobs.append(asyncio.create_task(manager._host_loop("h")))
            async with asyncio.timeout(20):
                while not manager.session(sid).connected or not manager.session(sid).budget_ready:  # noqa: ASYNC110
                    await asyncio.sleep(.03)
            await owner.call("turn/start", {"threadId": thread_id,
                "input": [{"type": "text", "text": "现在进入原任务的第二阶段，只回复约定的第二阶段代号，不运行工具。"}]})
            second = await owner.wait_event("turn/completed", thread_id=thread_id, timeout=180)
            assert second["params"]["turn"]["status"] == "completed", second
            output = "".join(event.get("params", {}).get("delta", "") for event in owner.history if event["method"] == "item/agentMessage/delta")
            assert "SECOND-FLASH-42" in output
            assert manager.task("A").requests_used == 2 and manager.task("A").tokens_used > 0
            await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "验证耗尽后拒绝请求"}]})
            rejected = await owner.wait_event("turn/completed", thread_id=thread_id, timeout=30)
            assert rejected["params"]["turn"]["status"] == "failed"
            assert manager.task("A").requests_used == 2
            result.update(passed=True, same_thread=manager.session(sid).thread_id == thread_id,
                daemon_restarted=True, independent_client_resumed_first=True, task_requests_charged=2,
                task_tokens_charged=manager.task("A").tokens_used, exhausted_budget_refused=True,
                retained_task_stages=["FIRST-FLASH-42", "SECOND-FLASH-42"])
    finally:
        if owner:
            await owner.close()
        await stop_server(process)
        manager.store.close()
        ledger = UsageLedger(ledger_path)
        result["cumulative_usage"] = ledger.summary()
        result["test_requests"] = result["cumulative_usage"]["requests"] - before["requests"]
        result["test_tokens"] = result["cumulative_usage"]["tokens"] - before["tokens"]
        rows = [dict(row) for row in ledger.db.execute("SELECT model,upstream_model,state FROM usage_requests ORDER BY created LIMIT -1 OFFSET ?", (before["requests"],))]
        result["request_sequence"] = rows
        result["accounting_consistent"] = (result["test_requests"] == 2
            and result["test_tokens"] == result.get("task_tokens_charged")
            and all(row["upstream_model"] == "deepseek-flash" for row in rows))
        result["passed"] = result["passed"] and result["accounting_consistent"]
        ledger.close()
        report = root / ".runtime/reports/live-managed-worker.json"
        await asyncio.to_thread(report.write_text, json.dumps(result, ensure_ascii=False, indent=2))
    assert result["passed"], result
