"""只重启本工具创建的专用 daemon；Windows 管理服务、网关和模拟上游均原生运行。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path

from super_brain.worker import worker_config, worker_token
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tools.test_windows_bridge import ps


async def main():
    root = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="super-brain-windows-budget-") as tmp:
        directory = Path(tmp)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        remote_port = listener.getsockname()[1]
        listener.close()
        endpoint = f"http://127.0.0.1:{remote_port}/inference/v1"
        process, sock = await start_server(directory, endpoint,
            config_text=worker_config(endpoint, worker_token("isolated-windows-budget", "windows-managed-test"), "A2-dsv41f"))
        remote = None
        try:
            config = {"id": "windows-managed-test", "name": "Linux 专用预算执行端", "address": "192.168.101.7", "username": "chiro",
                "codex_command": [codex_binary()], "control_socket": str(sock), "gateway_remote_port": remote_port,
                "known_hosts_file": "D:/Projects/super-brain/.runtime/platform-known-hosts", "allowed_roots": [tmp]}
            config_file = directory / "host.json"
            config_file.write_text(json.dumps(config), encoding="utf-8")
            await asyncio.to_thread(subprocess.run, ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                str(config_file), "192.168.101.5:D:/Projects/super-brain/.runtime/managed-host.json"], capture_output=True, check=True)
            command = """
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
$env:PYTHONUTF8='1'
Set-Location -LiteralPath 'D:\\Projects\\super-brain'
& uv run --frozen python -m tools.windows_managed_test --host-config .runtime/managed-host.json
exit $LASTEXITCODE
"""
            remote = await asyncio.create_subprocess_exec(*ps(command), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            async with asyncio.timeout(120):
                while line := await remote.stdout.readline():
                    message = line.decode(errors="replace").strip()
                    print(message, flush=True)
                    if message == "RESTART-ISOLATED-DAEMON":
                        await stop_server(process)
                        if sock.exists():
                            await asyncio.to_thread(sock.unlink)
                        process = await asyncio.create_subprocess_exec(codex_binary(), "app-server", "--listen", "unix://" + str(sock),
                            env={**os.environ, "CODEX_HOME": str(directory / "codex-home")}, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                        async with asyncio.timeout(15):
                            while not sock.exists():  # noqa: ASYNC110
                                await asyncio.sleep(.03)
                        remote.stdin.write(b"DAEMON-RESTARTED\n")
                        await remote.stdin.drain()
                assert await remote.wait() == 0, "Windows 原生预算重启测试失败"
            await asyncio.to_thread(subprocess.run, ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "192.168.101.5:D:/Projects/super-brain/.runtime/reports/windows-managed.json",
                str(root / ".runtime/reports/windows-managed.json")], capture_output=True, check=True)
        finally:
            if remote and remote.returncode is None:
                remote.kill()
                await remote.wait()
            await stop_server(process)


if __name__ == "__main__":
    asyncio.run(main())
