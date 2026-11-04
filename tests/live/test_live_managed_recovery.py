from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from super_brain.credentials import from_litellm
from super_brain.inference_proxy import inference_proxy
from super_brain.manager import Manager
from super_brain.models import Budget, ModelRoute, ServiceConfig, Session, StallPolicy, Task
from super_brain.rpc import CodexClient
from super_brain.store import Conflict, Store
from super_brain.usage import UsageLedger
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.test_api import serve_app

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SUPER_BRAIN_LIVE") != "1", reason="仅在明确启用真实联调时运行")]


async def test_real_manager_compacts_and_continues_original_file_task(tmp_path):
    credentials = from_litellm(Path("/home/chiro/litellm"))
    ledger = UsageLedger(".runtime/live-usage.db")
    before = ledger.summary()
    blocked = asyncio.Event()
    gate = [False]
    async def inject_stall(request, body):
        if gate[0] and body.get("model") == "A2-dsv41f":
            blocked.set()
            # 仅在专用代理挂起请求，不向生产后端制造拥堵，也不产生额外推理。
            while not await request.is_disconnected():  # noqa: ASYNC110
                await asyncio.sleep(.02)
            raise Conflict("隔离停滞请求已随客户端中断取消")
    proxy = inference_proxy(credentials.gateway_url, credentials.gateway_key, ledger,
        {name: "deepseek-flash" for name in ["A2-dsv41f", "A3-dsv41f", "ZL-DeepSeek-v4.1"]},
        before=inject_stall, max_output_tokens=8192)
    manager = Manager(ServiceConfig(), Store(":memory:"))
    for name, purpose in [("A3-dsv41f", "execution"), ("ZL-DeepSeek-v4.1", "recovery")]:
        manager.save("route", ModelRoute(id=name, name=name, purpose=purpose, opaque=True))
    original = json.dumps({"code": "ORBIT-42", "values": [4, 11, 27]}, ensure_ascii=False).encode()
    await asyncio.to_thread((tmp_path / "task_input.json").write_bytes, original)
    result = {"passed": False, "upstream_model": "deepseek-flash", "real_models": True,
        "role_mapping": "隔离角色映射，未调用生产 A2/A3/ZL", "stall_injection": "代理挂起未转发的请求；用模拟时钟确认长期无进展"}
    client = consumer = process = None
    try:
        async with serve_app(proxy) as url:
            process, sock = await start_server(tmp_path, url + "/v1")
            client = await CodexClient.unix(str(sock), codex_binary())
            manager.clients["h"] = client
            manager.create_task(Task(id="A", title="真实压缩后的文件交付", objective="读取 task_input.json，生成 result.json，JSON 字段恰为 code 和 sum；code 复制输入 code，sum 是输入 values 数组之和。输入文件保持不变。",
                cwd=str(tmp_path), allowed_roots=[str(tmp_path)], allowed_models=["A2-dsv41f", "A3-dsv41f"],
                acceptance="result.json 是合法 JSON，字段恰为 code 和 sum，值为输入 code 与数组和；task_input.json 字节不变。",
                budget=Budget(max_requests=25, max_tokens=1_000_000, max_retries=2)))
            thread = (await client.call("thread/start", {"cwd": str(tmp_path), "model": "A2-dsv41f", "modelProvider": "mock",
                "approvalPolicy": "never", "sandbox": "workspace-write", "config": {"agents.enabled": False}}))["thread"]
            sid = "h:" + thread["id"]
            manager.save("session", Session(id=sid, host_id="h", thread_id=thread["id"], task_id="A", cwd=str(tmp_path),
                model="A2-dsv41f", connected=True, control_enabled=True,
                stall_policy=StallPolicy(enabled=True, suspect_after=900, confirm_after=1800)))
            consumer = asyncio.create_task(manager._consume("h", client))
            cursor = manager.store.cursor()
            await manager.command(sid, "message", {"text": "原工作任务：读取 task_input.json，生成 result.json，JSON 字段必须恰为 code 和 sum；code 复制输入 code，sum 是输入 values 数组之和。输入文件不得修改，完成后验证 JSON 的值。当前准备阶段请只实际读取输入文件，记住数据并报告，不创建结果文件；后续管理器会发送继续指令推进交付。工作目录：" + str(tmp_path)}, "live-prepare")
            await manager.wait_codex_event(sid, cursor, lambda event:event["method"] == "turn/completed", timeout=240)
            gate[0] = True
            await manager.command(sid, "message", {"text": "继续原工作任务，准备产出并验证结果文件。"}, "live-stalled")
            await asyncio.wait_for(blocked.wait(), 30)
            manager.clock = lambda: time.time() + 4000
            manager.diagnose(sid, "model_backend", True, "专用隔离代理确认请求被挂起且无推进，模拟长期停滞")
            cursor = manager.store.cursor()
            recovered = await asyncio.wait_for(manager.recover(sid, "A3-dsv41f"), 240)
            result["recovery"] = recovered
            assert recovered["state"] in {"awaiting_progress", "complete"}, recovered
            await manager.wait_codex_event(sid, cursor, lambda event:event["method"] == "turn/completed" and event["params"]["turn"]["id"] == recovered["continuation_turn_id"], timeout=300)
            raw = await asyncio.to_thread((tmp_path / "result.json").read_text)
            artifact = json.loads(raw)
            assert set(artifact) == {"code", "sum"} and artifact["code"] == "ORBIT-42" and artifact["sum"] == 42, artifact
            assert await asyncio.to_thread((tmp_path / "task_input.json").read_bytes) == original
            evidence = [event for event in manager.store.decision_events(sid) if event["body"].get("params", {}).get("turnId") == recovered["continuation_turn_id"]
                and event["body"].get("params", {}).get("item", {}).get("type") == "commandExecution"
                and event["body"]["params"]["item"].get("exitCode") == 0]
            assert evidence, "接续后缺少实际成功工具事件"
            verification = manager.verify_task("A", True, "实际读取 result.json，code=ORBIT-42、sum=42，输入字节保持不变。", actor="manager", event_ids=[event["seq"] for event in evidence])
            rows = [dict(row) for row in ledger.db.execute("SELECT model,upstream_model,state,used FROM usage_requests ORDER BY created LIMIT -1 OFFSET ?", (before["requests"],))]
            assert any(row["model"] == "ZL-DeepSeek-v4.1" for row in rows)
            assert rows[-1]["model"] == "A3-dsv41f"
            result.update(passed=True, artifact=artifact, input_unchanged=True, task_state=verification["status"],
                recovery_state=manager.store.require("recovery", recovered["id"])["state"], request_sequence=[row["model"] for row in rows],
                all_upstream_deepseek_flash=all(row["upstream_model"] == "deepseek-flash" for row in rows))
    finally:
        if consumer:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        if client:
            await client.close()
        if process:
            await stop_server(process)
        result["cumulative_usage"] = ledger.summary()
        result["test_tokens"] = ledger.summary()["tokens"] - before["tokens"]
        report = Path(".runtime/reports/live-managed-recovery.json")
        await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(report.write_text, json.dumps(result, ensure_ascii=False, indent=2))
        ledger.close()
        manager.store.close()
