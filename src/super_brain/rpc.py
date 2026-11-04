"""Codex 换行 JSON-RPC：支持 stdio、Unix socket 和 SSH 控制代理。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import socket
import sys
from collections import deque
from typing import Any

from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    CloseConnection,
    Ping,
    RejectConnection,
    Request,
    TextMessage,
)


class RpcError(RuntimeError):
    def __init__(self, error: dict[str, Any]):
        self.error = error
        super().__init__(error.get("message", str(error)))


class RpcDisconnected(ConnectionError):
    pass


class CodexClient:
    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: Any = None
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4096)
        self.server_requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self.stderr: deque[str] = deque(maxlen=100)
        self.history: deque[dict] = deque(maxlen=256)
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self.connected = False
        self.resync_required = False
        self.ws: WSConnection | None = None
        self._ws_ready: asyncio.Future | None = None
        self._ws_text = ""

    @classmethod
    async def unix(cls, path: str, command: str = "codex") -> CodexClient:
        # proxy 透传 Unix WebSocket，stdio 上仍需完成 WebSocket 握手及帧编解码。
        return await cls.spawn([command, "app-server", "proxy", "--sock", path], websocket=True)

    @classmethod
    async def spawn(cls, argv: list[str], *, env: dict[str, str] | None = None,
                    websocket: bool = False) -> CodexClient:
        self = cls()
        if websocket:
            self.ws = WSConnection(ConnectionType.CLIENT)
            self._ws_ready = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, limit=16 * 1024 * 1024,
        )
        self.reader, self.writer = self.process.stdout, self.process.stdin
        self._start()
        self._tasks.append(asyncio.create_task(self._read_stderr()))
        try:
            if self.ws:
                await self._write(self.ws.send(Request(host="localhost", target="/")))
                await asyncio.wait_for(self._ws_ready, 10)
            await self.initialize()
        except Exception as exc:
            await self.close()
            detail = "; ".join(self.stderr)[-2000:]
            if detail:
                raise RpcDisconnected(f"{type(exc).__name__}: {exc}; proxy: {detail}") from exc
            raise
        except BaseException:
            await self.close()
            raise
        return self

    @classmethod
    async def ssh(cls, host: Any) -> CodexClient:
        if not host.address or host.address.startswith("-"):
            raise ValueError("SSH 目标无效")
        args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=yes", "-p", str(host.port)]
        if host.known_hosts_file:
            args += ["-o", f"UserKnownHostsFile={host.known_hosts_file}"]
        if host.identity_file:
            args += ["-i", host.identity_file]
        if host.jump:
            args += ["-J", host.jump]
        if host.gateway_remote_port and host.gateway_local_port:
            args += ["-o", "ExitOnForwardFailure=yes", "-R",
                     f"127.0.0.1:{host.gateway_remote_port}:127.0.0.1:{host.gateway_local_port}"]
        target = f"{host.username}@{host.address}" if host.username else host.address
        if host.transport == "tunnel" or (host.transport == "auto" and sys.platform == "win32"):
            return await cls.ssh_tunnel(args, target, host.control_socket)
        remote = list(host.codex_command)
        if host.codex_home:
            remote = ["env", f"CODEX_HOME={host.codex_home}", *remote]
        remote += ["app-server", "proxy", "--sock", host.control_socket]
        # SSH 的远端 command 由远端 shell 解释；每个参数必须按 Linux shell 引用。
        args += [target, shlex.join(remote)]
        return await cls.spawn(args, websocket=True)

    @classmethod
    async def ssh_tunnel(cls, args: list[str], target: str, control_socket: str) -> CodexClient:
        """Windows 使用 SSH TCP→远端 Unix socket 转发，避免原始 stdio 代理阻塞。"""
        self = cls()
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.ws = WSConnection(ConnectionType.CLIENT)
        self._ws_ready = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(
            *args, "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-L", f"127.0.0.1:{port}:{control_socket}", target,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tasks.append(asyncio.create_task(self._read_stderr()))
        try:
            async with asyncio.timeout(10):
                while True:
                    if self.process.returncode is not None:
                        raise RpcDisconnected("SSH 转发进程提前退出")
                    try:
                        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port, limit=16 * 1024 * 1024)
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
            self._start()
            await self._write(self.ws.send(Request(host="localhost", target="/")))
            await asyncio.wait_for(self._ws_ready, 10)
            await self.initialize()
            return self
        except BaseException as exc:
            await self.close()
            if isinstance(exc, Exception):
                raise RpcDisconnected(f"SSH 转发失败：{type(exc).__name__}: {exc}; {'; '.join(self.stderr)[-1500:]}") from exc
            raise

    def _start(self) -> None:
        self.connected = True
        self._tasks.append(asyncio.create_task(self._read()))

    async def initialize(self) -> dict:
        result = await self.call("initialize", {
            "clientInfo": {"name": "super_brain", "title": "Super Brain", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self.notify("initialized", {})
        return result

    async def _send(self, message: dict) -> None:
        if not self.connected:
            raise RpcDisconnected("Codex 控制连接已断开")
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        data = self.ws.send(TextMessage(data=text)) if self.ws else (text + "\n").encode()
        await self._write(data)

    async def _write(self, data: bytes) -> None:
        async with self._write_lock:
            self.writer.write(data)
            await self.writer.drain()

    async def call(self, method: str, params: dict | None = None, timeout: float = 30) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"method": method, "params": params})

    async def respond(self, request_id: Any, result: Any) -> None:
        await self._send({"id": request_id, "result": result})

    async def _read(self) -> None:
        reason = "Codex 控制连接结束"
        try:
            if self.ws:
                while data := await self.reader.read(65536):
                    self.ws.receive_data(data)
                    for event in self.ws.events():
                        if isinstance(event, AcceptConnection):
                            if not self._ws_ready.done():
                                self._ws_ready.set_result(True)
                        elif isinstance(event, RejectConnection):
                            raise RpcDisconnected(f"WebSocket 握手被拒绝：{event.status_code}")
                        elif isinstance(event, Ping):
                            await self._write(self.ws.send(event.response()))
                        elif isinstance(event, CloseConnection):
                            raise RpcDisconnected(f"WebSocket 结束：{event.reason}")
                        elif isinstance(event, TextMessage):
                            self._ws_text += event.data
                            if len(self._ws_text) > 16 * 1024 * 1024:
                                raise RpcDisconnected("WebSocket 消息超过限制")
                            if event.message_finished:
                                self._dispatch(json.loads(self._ws_text))
                                self._ws_text = ""
            else:
                while line := await self.reader.readline():
                    self._dispatch(json.loads(line))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"Codex 控制连接异常：{exc}"
        finally:
            self.connected = False
            if self._ws_ready and not self._ws_ready.done():
                self._ws_ready.set_exception(RpcDisconnected(reason))
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RpcDisconnected(reason))

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            self.history.append(message)
            queue = self.server_requests if "id" in message else self.events
            if queue.full():
                self.resync_required = True
                queue.get_nowait()
            queue.put_nowait(message)
        elif (future := self.pending.get(message.get("id"))) is not None and not future.done():
            if "error" in message:
                future.set_exception(RpcError(message["error"]))
            else:
                future.set_result(message.get("result"))

    async def _read_stderr(self) -> None:
        if self.process and self.process.stderr:
            while line := await self.process.stderr.readline():
                self.stderr.append(line.decode(errors="replace").rstrip())

    async def wait_event(self, method: str, *, thread_id: str | None = None,
                         timeout: float = 30) -> dict:
        return await self.wait_for(lambda event: event["method"] == method and (
            thread_id is None or event.get("params", {}).get("threadId") == thread_id
        ), timeout)

    async def wait_for(self, predicate, timeout: float = 30) -> dict:
        async def wait() -> dict:
            while True:
                event = await self.events.get()
                if predicate(event):
                    return event
        return await asyncio.wait_for(wait(), timeout)

    async def close(self) -> None:
        self.connected = False
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
