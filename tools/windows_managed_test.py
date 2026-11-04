"""Windows 原生网关通过 SSH 管理专用 Linux worker，验证 daemon 重启仍计账。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sys
from pathlib import Path

import uvicorn

from super_brain.api import create_app
from super_brain.manager import Manager
from super_brain.models import (
    Backend,
    Budget,
    Host,
    InferenceConfig,
    ModelRoute,
    ServiceConfig,
    Task,
)
from super_brain.store import Store
from tests.support.mock_model import MockModel


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-config", required=True)
    args = parser.parse_args()
    assert platform.system() == "Windows"
    host = Host.model_validate(json.loads(await asyncio.to_thread(Path(args.host_config).read_text, encoding="utf-8")))
    os.environ["SUPER_BRAIN_TOKEN"] = "isolated-windows-budget"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    mock = MockModel()
    async with mock.serve() as upstream:
        config = ServiceConfig(database=":memory:", port=port, poll_interval=1, reconnect_interval=1, hosts=[host],
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"),
            backends=[Backend(id="pool", name="Windows 隔离推理池", health="available")],
            routes=[ModelRoute(id="a", name="A2-dsv41f", backend_ids=["pool"], upstream_model="deepseek-flash")])
        manager = Manager(config, Store(":memory:"))
        server = uvicorn.Server(uvicorn.Config(create_app(config, manager), log_level="error"))
        job = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(manager.host_ready.setdefault(host.id, asyncio.Event()).wait(), 30)
            manager.create_task(Task(id="A", title="Windows 重启预算验收", objective="仅返回隔离测试进度", host_id=host.id,
                cwd=host.allowed_roots[0], allowed_roots=host.allowed_roots, allowed_models=["A2-dsv41f"],
                budget=Budget(max_requests=2, max_tokens=100000)))
            cursor = manager.store.cursor()
            dispatched = await manager.dispatch("A")
            sid = dispatched["session_id"]
            await manager.wait_codex_event(sid, cursor, lambda event: event["method"] == "turn/completed")
            assert len(mock.requests) == manager.task("A").requests_used == 1
            cursor = manager.store.cursor()
            print("RESTART-ISOLATED-DAEMON", flush=True)
            reply = await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), 25)
            assert reply.strip() == "DAEMON-RESTARTED"
            async with asyncio.timeout(30):
                while not any(event["kind"] == "budget.worker_restored" for event in manager.store.events(cursor, 1000, sid)) or not manager.session(sid).budget_ready:  # noqa: ASYNC110
                    await asyncio.sleep(.05)
            cursor = manager.store.cursor()
            await manager.command(sid, "message", {"text": "Windows 在执行端重启后继续同一任务"}, "windows-resume", actor="user")
            await manager.wait_codex_event(sid, cursor, lambda event: event["method"] == "turn/completed")
            assert len(mock.requests) == manager.task("A").requests_used == 2
            cursor = manager.store.cursor()
            await manager.clients[host.id].call("turn/start", {"threadId": manager.session(sid).thread_id,
                "input": [{"type": "text", "text": "预算耗尽后的原生客户端请求"}]})
            rejected = await manager.wait_codex_event(sid, cursor, lambda event: event["method"] == "turn/completed")
            assert rejected["params"]["turn"]["status"] == "failed" and len(mock.requests) == 2
            result = {"passed": True, "native_windows": True, "platform": platform.platform(),
                "real_models": False, "ssh_reverse_gateway": True, "daemon_restart": True,
                "same_thread": manager.session(sid).thread_id == sid.split(":", 1)[1],
                "charged_requests": manager.task("A").requests_used, "upstream_requests": len(mock.requests),
                "exhausted_budget_refused": True}
            target = Path(".runtime/reports/windows-managed.json")
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_text, json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            server.should_exit = True
            await asyncio.wait_for(job, 15)
            listener.close()
            manager.store.close()


if __name__ == "__main__":
    asyncio.run(main())
