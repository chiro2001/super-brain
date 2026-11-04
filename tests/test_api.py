from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import uvicorn
from fastapi.testclient import TestClient
from mcp import Client

from super_brain.api import create_app
from super_brain.manager import Manager
from super_brain.models import ServiceConfig
from super_brain.store import Store


def app_and_manager(monkeypatch, token=None):
    if token:
        monkeypatch.setenv("SUPER_BRAIN_TOKEN", token)
    else:
        monkeypatch.delenv("SUPER_BRAIN_TOKEN", raising=False)
    config = ServiceConfig(database=":memory:")
    manager = Manager(config, Store(":memory:"))
    return create_app(config, manager), manager


def test_browser_and_text_report_share_task_state(monkeypatch):
    app, manager = app_and_manager(monkeypatch)
    with TestClient(app) as client:
        result = client.post("/api/tasks", json={"title": "A1 中文任务", "allowed_roots": ["/work"], "cwd": "/work/a"})
        assert result.status_code == 200, result.text
        task_id = result.json()["id"]
        assert client.get("/api/snapshot").json()["tasks"][0]["id"] == task_id
        assert "A1 中文任务" in client.get("/api/report").json()["text"]
        result = client.post("/api/manager-action", json={"tool": "task_update", "arguments": {"task_id": task_id, "patch": {"allowed_roots": ["/"]}}})
        assert result.json()["approval_required"]
        proposal_id = result.json()["proposal"]["id"]
        assert client.get("/api/snapshot").json()["tasks"][0]["allowed_roots"] == ["/work"]
        assert client.post(f"/api/proposals/{proposal_id}/decide", json={"approved": False}).json()["state"] == "rejected"
    manager.store.close()


def test_token_cookie_and_foreign_origin_control(monkeypatch):
    app, manager = app_and_manager(monkeypatch, "test-private-token")
    with TestClient(app) as client:
        assert client.get("/api/snapshot").status_code == 401
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"token": "test-private-token"}).status_code == 200
        assert client.get("/api/snapshot").status_code == 200
        assert client.post("/api/tasks", json={"title": "bad"}, headers={"Origin": "http://other-site"}).status_code == 403
        assert client.post("/api/tasks", json={"title": "good"}, headers={"Origin": "http://testserver"}).status_code == 200
    manager.store.close()


def test_loopback_without_token_also_rejects_cross_site_writes(monkeypatch):
    app, manager = app_and_manager(monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/tasks", json={"title": "跨站请求"}, headers={"Origin": "http://other-site"}).status_code == 403
        assert not manager.store.list("task")
    manager.store.close()


@asynccontextmanager
async def serve_app(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def test_real_mcp_sdk_can_query_and_manage_tasks(monkeypatch):
    app, manager = app_and_manager(monkeypatch)
    async with serve_app(app) as url:
        async with Client(url + "/mcp") as client:
            tools = await client.list_tools()
            assert any(tool.name == "work_snapshot" for tool in tools.tools)
            result = await client.call_tool("task_create", {"title": "通过标准 MCP 录入任务"})
            assert not result.is_error
            parent = manager.store.list("task")[0]
            result = await client.call_tool("task_create", {"title": "MCP 分配的紧急子任务", "parent_id": parent["id"], "priority": 80})
            assert not result.is_error
            child = manager.store.list("task")[0]
            assert child["parent_id"] == parent["id"] and child["priority"] == 80
            result = await client.call_tool("work_snapshot", {})
            assert not result.is_error
            assert "通过标准 MCP 录入任务" in str(result.structured_content)
            task = manager.store.list("task")[0]
            result = await client.call_tool("task_update", {"task_id": task["id"], "patch": {"budget": {"max_requests": 999}}})
            assert "approval_required" in str(result.structured_content)
            assert manager.task(task["id"]).budget.max_requests == 20
    manager.store.close()
