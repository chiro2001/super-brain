"""本机创建隔离 Linux Codex，交给 gamepc 原生服务经 SSH 控制。"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import tempfile
from pathlib import Path

from super_brain.rpc import CodexClient
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel


def ps(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
            "192.168.101.5", "pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


async def main():
    root = Path.cwd()
    state = root / ".runtime/windows-bridge"
    state.mkdir(parents=True, exist_ok=True)
    host_key = (await asyncio.to_thread(Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text)).split()
    known = state / "known_hosts"
    known.write_text("192.168.101.7 " + " ".join(host_key[:2]) + "\n")
    with tempfile.TemporaryDirectory(prefix="super-brain-platform-") as tmp:
        tmp_path = Path(tmp)
        mock = MockModel()
        async with mock.serve() as url:
            process, sock = await start_server(tmp_path, url)
            owner = None
            try:
                owner = await CodexClient.unix(str(sock), codex_binary())
                result = await owner.call("thread/start", {"model": "A2-dsv41f", "modelProvider": "mock", "cwd": str(tmp_path)})
                thread_id = result["thread"]["id"]
                await owner.call("thread/goal/set", {"threadId": thread_id, "objective": "验证 Windows 管理端控制 Linux 会话", "origin": "user", "status": "paused"})
                await owner.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "初始化平台验收会话。"}]})
                await owner.wait_event("turn/completed", thread_id=thread_id)
                config = {"id": "linux-platform-test", "name": "Linux 隔离测试端", "address": "192.168.101.7",
                          "username": "chiro", "codex_command": [codex_binary()], "control_socket": str(sock),
                          "known_hosts_file": "D:/Projects/super-brain/.runtime/platform-known-hosts",
                          "allowed_roots": [str(tmp_path)]}
                config_file = state / "host.json"
                config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                await asyncio.to_thread(subprocess.run, ps("$ProgressPreference='SilentlyContinue'; New-Item -ItemType Directory -Force -Path 'D:\\Projects\\super-brain\\.runtime' | Out-Null"), check=True, capture_output=True)
                for source, target in [
                    (root / "tools/windows_platform_test.py", "D:/Projects/super-brain/windows_platform_test.py"),
                    (known, "D:/Projects/super-brain/.runtime/platform-known-hosts"),
                    (config_file, "D:/Projects/super-brain/.runtime/platform-host.json"),
                ]:
                    await asyncio.to_thread(subprocess.run, ["scp", "-o", "BatchMode=yes", str(source), "192.168.101.5:" + target], check=True, capture_output=True)
                command = """
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
$ProgressPreference='SilentlyContinue'
$env:PYTHONUTF8='1'
Set-Location -LiteralPath 'D:\\Projects\\super-brain'
& uv run --frozen python windows_platform_test.py --host-config .runtime/platform-host.json
exit $LASTEXITCODE
"""
                # 必须异步等待，隔离模型服务与 Codex 事件循环仍需持续运行。
                remote = await asyncio.create_subprocess_exec(*ps(command), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                output = []
                async with asyncio.timeout(120):
                    while line := await remote.stdout.readline():
                        value = line.decode("utf-8", errors="replace")
                        output.append(value)
                        print(value, end="", flush=True)
                    code = await remote.wait()
                report = state / "remote-output.txt"
                report.write_text("".join(output))
                assert code == 0, f"Windows 平台测试失败，见 {report}"
                assert len(mock.requests) >= 2, "Windows 没有实际触发第二次 Codex 请求"
                reports = root / ".runtime/reports"
                await asyncio.to_thread(reports.mkdir, parents=True, exist_ok=True)
                for name, target in [("result.json", "windows-native.json"), ("dashboard.png", "windows-dashboard.png")]:
                    await asyncio.to_thread(subprocess.run, ["scp", "-o", "BatchMode=yes", "192.168.101.5:D:/Projects/super-brain/.runtime/windows-platform/" + name, str(reports / target)], capture_output=True, check=True)
                print(json.dumps({"linux_thread": thread_id, "model_requests": len(mock.requests), "bridge_passed": True}))
            finally:
                if owner:
                    await owner.close()
                await stop_server(process)


if __name__ == "__main__":
    asyncio.run(main())
