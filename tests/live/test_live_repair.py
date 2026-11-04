from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from super_brain.credentials import from_litellm
from super_brain.inference_proxy import inference_proxy
from super_brain.manager import Manager
from super_brain.models import Budget, Host, ServiceConfig, Session, Task
from super_brain.rpc import CodexClient
from super_brain.store import Store
from super_brain.supervisor import Supervisor
from super_brain.usage import UsageLedger
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.test_api import serve_app

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SUPER_BRAIN_LIVE") != "1", reason="仅在明确启用真实联调时运行")]


async def test_real_model_task_failure_supervisor_repair_and_verification(tmp_path):
    credentials = from_litellm(Path("/home/chiro/litellm"))
    ledger = UsageLedger(".runtime/live-usage.db")
    before = ledger.summary()
    proxy = inference_proxy(credentials.gateway_url, credentials.gateway_key, ledger,
                            {"execution": "deepseek-flash", "supervisor": "deepseek-flash"}, max_output_tokens=8192)
    (tmp_path / "solution.py").write_text('def clamp(value, lower, upper):\n    """把 value 限制在闭区间 [lower, upper]。"""\n    return max(value, lower)\n')
    (tmp_path / "test_solution.py").write_text(
        'import unittest\nfrom solution import clamp\nclass TestClamp(unittest.TestCase):\n'
        '    def test_bounds(self):\n        self.assertEqual(clamp(15, 0, 10), 10)\n'
        '        self.assertEqual(clamp(-5, 0, 10), 0)\n        self.assertEqual(clamp(5, 0, 10), 5)\n')
    config = ServiceConfig(supervisor={"kind": "codex", "host_id": "h", "model": "supervisor", "model_provider": "mock",
        "cwd": str(tmp_path), "max_invocations": 3})
    manager = Manager(config, Store(":memory:"))
    result = {"passed": False, "upstream_model": "deepseek-flash", "real_models": True}
    try:
        async with serve_app(proxy) as url:
            process, sock = await start_server(tmp_path, url + "/v1")
            client = consumer = None
            try:
                client = await CodexClient.unix(str(sock), codex_binary())
                manager.clients["h"] = client
                manager.save("host", Host(id="h", name="真实模型隔离测试", address="localhost", allowed_roots=[str(tmp_path)]))
                manager.create_task(Task(id="repair", title="修复 clamp 上边界", objective="修复 solution.py 的 clamp，使值始终位于给定闭区间。保持测试用例不变。",
                    cwd=str(tmp_path), allowed_roots=[str(tmp_path)], acceptance="python -m unittest -q 全部通过，test_solution.py 内容保持不变。",
                    budget=Budget(max_requests=30, max_tokens=1_000_000, max_retries=2)))
                original_test = (tmp_path / "test_solution.py").read_bytes()
                thread = (await client.call("thread/start", {"cwd": str(tmp_path), "model": "execution", "modelProvider": "mock",
                    "approvalPolicy": "never", "sandbox": "workspace-write", "config": {"agents.enabled": False}}))["thread"]
                sid = "h:" + thread["id"]
                manager.save("session", Session(id=sid, host_id="h", thread_id=thread["id"], task_id="repair", cwd=str(tmp_path),
                    model="execution", connected=True, control_enabled=True))
                consumer = asyncio.create_task(manager._consume("h", client))
                cursor = manager.store.cursor()
                await manager.command(sid, "message", {"text": f"原工作目标：修复 solution.py 中 clamp 上边界错误，保持 test_solution.py 不变。当前是诊断阶段：请只运行 {sys.executable} -m unittest -q 并报告真实结果，本回合不要改文件，修复阶段由管理器后续安排。工作目录 {tmp_path}。"}, "live-initial")
                await manager.wait_codex_event(sid, cursor, lambda e:e["method"] == "turn/completed", timeout=240)
                jobs = [job for job in manager.store.list("decision") if job["source"] == "tool_error" and job["state"] == "pending"]
                assert jobs, "真实模型没有执行并记录预期失败的测试"
                cursor = manager.store.cursor()
                repair = await Supervisor(manager).process(jobs[0]["id"])
                result["repair_decision"] = repair
                assert repair["state"] == "complete" and repair["decision"]["action"] == "repair", json.dumps(repair, ensure_ascii=False)
                await manager.wait_codex_event(sid, cursor, lambda e:e["method"] == "turn/completed", timeout=300)
                check = await asyncio.create_subprocess_exec(sys.executable, "-m", "unittest", "-q", cwd=str(tmp_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                output, _ = await check.communicate()
                assert check.returncode == 0, output.decode(errors="replace")
                assert (tmp_path / "test_solution.py").read_bytes() == original_test
                jobs = [job for job in manager.store.list("decision") if job["source"] == "verify" and job["state"] == "pending"]
                assert jobs
                verified = await Supervisor(manager).process(jobs[0]["id"])
                result["verification_decision"] = verified
                assert verified["state"] == "complete" and manager.task("repair").status == "complete", json.dumps(verified, ensure_ascii=False)
                result.update(passed=True, original_thread_preserved=True, actual_test_output=output.decode(errors="replace"),
                    test_file_unchanged=True, supervisor_invocations=manager.store.require("supervisor", "codex")["invocations"],
                    retries_used=manager.task("repair").retries_used, verification=manager.task("repair").verification)
            finally:
                if consumer:
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
                if client:
                    await client.close()
                await stop_server(process)
    finally:
        result["cumulative_usage"] = ledger.summary()
        result["test_tokens"] = ledger.summary()["tokens"] - before["tokens"]
        report = Path(".runtime/reports/live-repair.json")
        await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(report.write_text, json.dumps(result, ensure_ascii=False, indent=2))
        ledger.close()
        manager.store.close()
