"""在 gamepc 的原生 Python 中运行；只操作本脚本创建的服务进程。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import yaml
from mcp import Client
from playwright.async_api import async_playwright

from tests.support.mock_model import MockModel, function_call


async def codex_mcp_consumer(url: str, run_dir: Path) -> dict:
    """独立 Windows Codex 加载交付的 skill，并实际调用 HTTP MCP。"""
    mock = MockModel()
    invoked = []
    tool_results = []
    def handler(body):
        if invoked:
            tool_results.extend(item for item in body.get("input", []) if item.get("type") == "function_call_output")
            mock.text = "WINDOWS-CODEX-MCP-OK"
            return None
        for tool in body.get("tools", []):
            for candidate in tool.get("tools", [tool]):
                name = candidate.get("name", "")
                if "work_snapshot" in name:
                    invoked.append(name)
                    namespace = tool["name"] if "tools" in tool else None
                    return function_call(name, {}, namespace)
        raise AssertionError("Windows Codex 未发现 MCP work_snapshot 工具")
    mock.handler = handler
    home = run_dir / ("consumer-" + uuid.uuid4().hex)
    home.mkdir()
    skill = Path.cwd() / ".agents/skills/super-brain-manager"
    shutil.copytree(skill, home / "skills/super-brain-manager")
    async with mock.serve() as model_url:
        (home / "config.toml").write_text(
            'model_provider="mock"\nmodel="test"\ncheck_for_update_on_startup=false\n'
            '[model_providers.mock]\nname="隔离验收"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'base_url="{model_url}"\nrequest_max_retries=0\nstream_max_retries=0\n[mcp_servers.super_brain]\nurl="{url}/mcp"\ndefault_tools_approval_mode="writes"\n'
            '[analytics]\nenabled=false\n', encoding="utf-8")
        env = {**os.environ, "CODEX_HOME": str(home), "PYTHONUTF8": "1"}
        binary = shutil.which("codex")
        assert binary, "Windows 未找到 Codex CLI"
        log_path = home / "consumer.jsonl"
        with log_path.open("wb") as output:
            process = await asyncio.to_thread(subprocess.Popen, [binary, "exec", "--skip-git-repo-check",
                "--sandbox", "read-only", "--json", "-C", str(home),
                "$super-brain-manager 请调用 work_snapshot 获取当前快速工作看板，不修改任务。"],
                env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                code = await asyncio.wait_for(asyncio.to_thread(process.wait), 60)
            finally:
                if process.poll() is None:
                    await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                    await asyncio.to_thread(process.wait, timeout=10)
        output_text = log_path.read_text(encoding="utf-8", errors="replace")
        assert code == 0 and "WINDOWS-CODEX-MCP-OK" in output_text, output_text[-6000:]
        assert invoked
        assert "Windows 原生跨主机验收" in json.dumps(tool_results, ensure_ascii=False), json.dumps(tool_results, ensure_ascii=False)[-5000:]
        inputs = json.dumps(mock.requests[0].get("input", []), ensure_ascii=False)
        assert "以实际工作任务为中心" in inputs, "Windows Codex 未加载 skill 正文"
        return {"native_codex_consumer": True, "skill_loaded": True, "mcp_tool_called": invoked[0], "real_model_calls": 0}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-config", required=True)
    args = parser.parse_args()
    assert platform.system() == "Windows", "该验收必须在 Windows 本机执行"
    root = Path.cwd()
    run_dir = root / ".runtime/windows-platform"
    run_dir.mkdir(parents=True, exist_ok=True)
    host = json.loads(await asyncio.to_thread(Path(args.host_config).read_text, encoding="utf-8"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = {"database": str(run_dir / f"state-{uuid.uuid4().hex}.db"), "port": port,
              "token_env": "SUPER_BRAIN_PLATFORM_TEST_TOKEN", "poll_interval": 1,
              "hosts": [host]}
    config_path = run_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    env = dict(os.environ)
    env.pop("SUPER_BRAIN_PLATFORM_TEST_TOKEN", None)
    log = await asyncio.to_thread(open, run_dir / "service.log", "wb")
    process = await asyncio.to_thread(subprocess.Popen, [sys.executable, "-m", "super_brain.cli", "serve", "--config", str(config_path)],
                                      stdout=log, stderr=subprocess.STDOUT, env=env)
    url = f"http://127.0.0.1:{port}"
    result = {"platform": platform.platform(), "python": sys.version.split()[0],
              "native_windows": True, "pid": process.pid, "passed": False}
    result["codex_version"] = (await asyncio.to_thread(subprocess.check_output, [shutil.which("codex"), "--version"], text=True)).strip()
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            async with asyncio.timeout(30):
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("服务提前退出：" + (run_dir / "service.log").read_text(errors="replace")[-3000:])
                    try:
                        response = await http.get(url + "/health")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
            async with asyncio.timeout(30):
                while True:
                    snapshot = (await http.get(url + "/api/snapshot")).json()
                    result["last_host_status"] = snapshot["hosts"]
                    if snapshot["sessions"] and snapshot["sessions"][0]["connected"] and snapshot["sessions"][0]["cwd"] and snapshot["sessions"][0].get("goal"):
                        break
                    await asyncio.sleep(0.2)
            session = snapshot["sessions"][0]
            assert session["host_id"] == host["id"]
            assert snapshot["hosts"][0]["connection"] == "connected", snapshot["hosts"][0]
            async with Client(url + "/mcp") as client:
                tools = await client.list_tools()
                assert any(t.name == "session_message" for t in tools.tools)
                task = (await http.post(url + "/api/tasks", json={"title": "Windows 原生跨主机验收",
                    "host_id": host["id"], "cwd": session["cwd"], "allowed_roots": host["allowed_roots"],
                    "budget": {"max_requests": 5}})).json()
                bound = await client.call_tool("session_bind", {"session_id": session["id"], "task_id": task["id"], "control_enabled": True})
                assert not bound.is_error, bound
                before = (await http.get(url + "/api/snapshot")).json()["cursor"]
                command = await client.call_tool("session_message", {"session_id": session["id"],
                    "text": "Windows 原生管理端通过 MCP 和 SSH 发送的隔离验证指令。返回当前测试进度。",
                    "operation_id": uuid.uuid4().hex})
                assert not command.is_error, command
                async with asyncio.timeout(20):
                    while True:
                        events = (await http.get(url + "/api/events", params={"after": before, "entity_id": session["id"], "limit": 1000})).json()
                        if any(e["kind"] == "codex.event" and e["body"]["method"] == "turn/completed" for e in events):
                            break
                        await asyncio.sleep(0.1)
                report = await client.call_tool("work_report", {})
                assert "Windows 原生跨主机验收" in str(report)
            assert (await http.get(url + "/")).status_code == 200
            result.update(service_started=True, mcp_passed=True, windows_to_linux_ssh=True,
                          controlled_thread=session["thread_id"], goal_observed=bool(session.get("goal")))
            result.update(await codex_mcp_consumer(url, run_dir))
            edge = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
            if edge.is_file():
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(channel="msedge", headless=True)
                    page = await browser.new_page(viewport={"width": 1600, "height": 1000})
                    await page.goto(url)
                    await page.get_by_role("heading", name="所有工作，一眼掌握。").wait_for()
                    await page.locator(".session-card").first.wait_for()
                    await page.screenshot(path=str(run_dir / "dashboard.png"), full_page=True)
                    result["native_browser_passed"] = True
                    await browser.close()
            else:
                result["native_browser_passed"] = False
                result["browser_note"] = "未发现原生 Edge，服务及跨主机控制测试结果独立保留"
            result["passed"] = True
    finally:
        if process.poll() is None:
            await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            await asyncio.to_thread(process.wait, timeout=10)
        log.close()
        result["finished_at"] = time.time()
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
