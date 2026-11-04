from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

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
from super_brain.worker import worker_config
from tests.integration.test_codex_protocol import codex_binary, start_server, stop_server
from tests.support.mock_model import MockModel, function_call
from tests.test_api import serve_app

pytestmark = pytest.mark.integration


async def test_native_agent_limit_and_inherited_task_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "agent-budget-admin")
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 SSH localhost 的测试主机密钥")
    mock = MockModel()
    release_children = asyncio.Event()
    parent_calls = 0
    child_calls = []

    async def handler(body):
        nonlocal parent_calls
        user_text = " ".join(str(item.get("content", "")) for item in body.get("input", []) if item.get("role") == "user")
        if "CHILD-HOLD" in user_text and "PARENT-QUOTA" not in user_text:
            child_calls.append(body)
            await release_children.wait()
            return None
        parent_calls += 1
        if parent_calls <= 2:
            namespace = next((tool["name"] for tool in body.get("tools", []) if any(child.get("name") == "spawn_agent" for child in tool.get("tools", []))), None)
            return function_call("spawn_agent", {"message": ("FIRST-" if parent_calls == 1 else "SECOND-") + "CHILD-HOLD", "fork_context": False}, namespace)
        return None

    mock.handler = handler
    async with mock.serve() as upstream:
        process, sock = None, tmp_path / "control.sock"
        config = ServiceConfig(database=":memory:", poll_interval=1,
            hosts=[Host(id="local", name="配额测试端", address="localhost", control_socket=str(sock),
                        known_hosts_file=str(known), codex_command=[codex_binary()], allowed_roots=[str(tmp_path)])],
            backends=[Backend(id="pool", name="共享模型后端", max_requests=3, health="available")],
            routes=[ModelRoute(id="a", name="A2-dsv41f", backend_ids=["pool"], upstream_model="deepseek-flash")],
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"))
        manager = Manager(config, Store(":memory:"))
        app = create_app(config, manager)
        try:
            async with serve_app(app) as url:
                config.inference.public_url = url
                process, sock = await start_server(tmp_path, url + "/inference/v1",
                    config_text=worker_config(url + "/inference/v1", manager.gateway.worker_token("local"), "A2-dsv41f"))
                await asyncio.wait_for(manager.host_ready.setdefault("local", asyncio.Event()).wait(), 15)
                task = Task(id="parent", title="PARENT-QUOTA 子代理上限验收", objective="这是隔离测试，用户明确要求尝试启动两名测试子代理来验证上限，不修改任何文件。",
                    host_id="local", cwd=str(tmp_path), allowed_roots=[str(tmp_path)], allowed_models=["A2-dsv41f"],
                    budget=Budget(max_requests=6, max_agents=1))
                manager.create_task(task)
                cursor = manager.store.cursor()
                dispatch = await manager.dispatch(task.id)
                await manager.wait_codex_event(dispatch["session_id"], cursor, lambda e:e["method"] == "turn/completed", timeout=30)
                assert parent_calls >= 3, parent_calls
                assert len(child_calls) == 1, f"实际子代理请求数 {len(child_calls)}，预期只允许 1 名"
                assert len(mock.requests) == 4, [(r["model"],str(r.get("input"))[-100:]) for r in mock.requests]
                assert manager.task(task.id).requests_used == len(mock.requests), "子代理没有继承受管网关预算"
                assert all(request["model"] == "deepseek-flash" for request in mock.requests)
                second_attempt = json.dumps(mock.requests[-1].get("input", []), ensure_ascii=False).lower()
                assert "limit" in second_attempt or "maximum" in second_attempt, second_attempt[-2000:]
                report = Path(__file__).parents[2] / ".runtime/reports/agent-budget.json"
                report.write_text(json.dumps({"passed": True, "max_agents": 1, "actual_children": len(child_calls),
                    "parent_model_requests": parent_calls, "all_model_requests": len(mock.requests),
                    "task_requests_charged": manager.task(task.id).requests_used}, ensure_ascii=False, indent=2))
                release_children.set()
                await asyncio.sleep(0.2)
        finally:
            release_children.set()
            manager.store.close()
            await stop_server(process)
