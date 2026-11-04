from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from super_brain.credentials import from_litellm
from super_brain.inference_proxy import inference_proxy
from super_brain.rpc import CodexClient
from super_brain.usage import UsageLedger
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.test_api import serve_app

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SUPER_BRAIN_LIVE") != "1", reason="仅在明确启用真实联调时运行")]


async def test_real_deepseek_context_compaction_and_continuation(tmp_path):
    """真实推理协议验收；执行与压缩角色均映射到获授权的 deepseek-flash。"""
    root = Path(os.environ.get("SUPER_BRAIN_LITELLM_DIR", "/home/chiro/litellm"))
    credentials = from_litellm(root)
    state_root = Path(__file__).parents[2] / ".runtime"
    ledger = UsageLedger(str(state_root / "live-usage.db"))
    proxy = inference_proxy(credentials.gateway_url, credentials.gateway_key, ledger, {
        "A2-dsv41f": "deepseek-flash", "A3-dsv41f": "deepseek-flash", "ZL-DeepSeek-v4.1": "deepseek-flash",
    })
    result = {"test": "真实 Codex 压缩与接续协议", "upstream_model": "deepseek-flash",
              "role_mapping": "隔离测试路由映射，不代表实际生产 ZL 后端已验证", "passed": False}
    try:
        async with serve_app(proxy) as url:
            process, sock = await start_server(tmp_path, url + "/v1")
            owner = manager = None
            try:
                owner = await CodexClient.unix(str(sock), codex_binary())
                thread = (await owner.call("thread/start", {"model": "A2-dsv41f", "modelProvider": "mock",
                    "cwd": str(tmp_path), "approvalPolicy": "never", "sandbox": "read-only"}))["thread"]
                thread_id = thread["id"]
                await owner.call("thread/goal/set", {"threadId": thread_id,
                    "objective": "验证真实模型压缩接续并保留任务事实", "status": "paused", "origin": "user"})
                await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text",
                    "text": "这是一个隔离协议测试。请记住任务代号 ORBIT-42。不要运行工具，仅回复：已记住任务代号。"}]})
                manager = await CodexClient.unix(str(sock), codex_binary())
                await manager.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                await owner.wait_event("turn/completed", thread_id=thread_id, timeout=180)
                await manager.call("thread/settings/update", {"threadId": thread_id, "model": "ZL-DeepSeek-v4.1"})
                await manager.call("thread/compact/start", {"threadId": thread_id})
                await manager.wait_for(lambda e: e["method"] == "item/completed" and e.get("params", {}).get("item", {}).get("type") == "contextCompaction", timeout=180)
                await manager.wait_event("turn/completed", thread_id=thread_id, timeout=180)
                await manager.call("thread/settings/update", {"threadId": thread_id, "model": "A3-dsv41f"})
                await manager.call("turn/start", {"threadId": thread_id, "input": [{"type": "text",
                    "text": "请仅返回压缩之前要求你记住的任务代号。不要运行工具，也不要解释。"}]})
                await manager.wait_event("turn/completed", thread_id=thread_id, timeout=180)
                text = "".join(str(e.get("params", {}).get("delta", "")) for e in manager.history if e["method"] == "item/agentMessage/delta")
                assert "ORBIT-42" in text, "压缩后没有正确保留任务事实"
                rows = [dict(row) for row in ledger.db.execute("SELECT model,upstream_model,state,used,estimated FROM usage_requests ORDER BY created")]
                sequence = [row["model"] for row in rows]
                assert "ZL-DeepSeek-v4.1" in sequence and sequence[-1] == "A3-dsv41f", sequence
                assert all(row["upstream_model"] == "deepseek-flash" for row in rows)
                result.update(passed=True, thread_id=thread_id, request_sequence=sequence,
                              retained_fact="ORBIT-42", usage=ledger.summary())
            finally:
                if manager:
                    await manager.close()
                if owner:
                    await owner.close()
                await stop_server(process)
    finally:
        result["usage"] = ledger.summary()
        report = state_root / "reports/live-protocol.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        ledger.close()
