from __future__ import annotations

import asyncio

import httpx

from super_brain.api import create_app
from super_brain.manager import Manager
from super_brain.models import Backend, Budget, InferenceConfig, ModelRoute, ServiceConfig, Task
from super_brain.store import Store
from tests.support.mock_model import MockModel
from tests.test_api import serve_app


async def test_managed_gateway_enforces_shared_task_and_child_request_budget(monkeypatch):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "admin-secret")
    mock = MockModel()
    mock.delay = 0.05
    async with mock.serve() as upstream:
        config = ServiceConfig(database=":memory:",
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"),
            backends=[Backend(id="pool", name="共享池", max_requests=1, health="available")],
            routes=[ModelRoute(id="a", name="A2-dsv41f", backend_ids=["pool"], upstream_model="deepseek-flash")])
        manager = Manager(config, Store(":memory:"))
        manager.create_task(Task(id="A", title="父子会话共享预算", status="running", allowed_models=["A2-dsv41f"],
                                 budget=Budget(max_requests=2, max_tokens=100000)))
        app = create_app(config, manager)
        async with serve_app(app) as url:
            async with httpx.AsyncClient(timeout=10) as http:
                bad = await http.post(url + "/inference/v1/responses", json={"model": "A2-dsv41f"})
                assert bad.status_code == 429
                assert not mock.requests
                headers = {"Authorization": "Bearer " + manager.gateway.token("A")}
                assert (await http.get(url + "/api/snapshot", headers=headers)).status_code == 401
                async def request():
                    return await http.post(url + "/inference/v1/responses", headers=headers,
                                           json={"model": "A2-dsv41f", "input": "执行或子代理的请求", "stream": True})
                responses = await asyncio.gather(request(), request(), request())
                assert sorted(r.status_code for r in responses) == [200, 200, 429]
                assert len(mock.requests) == 2
                assert manager.task("A").requests_used == 2
                assert manager.task("A").tokens_used == 140
                assert manager.task("A").tokens_reserved == 0
                assert not manager.store.active_inference()
                assert all(r["model"] == "deepseek-flash" for r in mock.requests)
        manager.store.close()


async def test_recovery_only_model_is_not_a_normal_execution_backdoor(monkeypatch):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "admin-secret")
    mock = MockModel()
    async with mock.serve() as upstream:
        config = ServiceConfig(database=":memory:",
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"),
            routes=[ModelRoute(id="z", name="ZL-DeepSeek-v4.1", purpose="recovery", opaque=True, upstream_model="deepseek-flash")])
        manager = Manager(config, Store(":memory:"))
        manager.create_task(Task(id="A", title="普通工作", status="running", budget=Budget(max_requests=5)))
        app = create_app(config, manager)
        async with serve_app(app) as url:
            async with httpx.AsyncClient() as http:
                response = await http.post(url + "/inference/v1/responses",
                    headers={"Authorization": "Bearer " + manager.gateway.token("A")},
                    json={"model": "ZL-DeepSeek-v4.1"})
                assert response.status_code == 429
                assert not mock.requests
        manager.store.close()


async def test_restart_settles_unknown_requests_without_refunding_usage(tmp_path):
    path = str(tmp_path / "restart-budget.db")
    first = Manager(ServiceConfig(database=path))
    first.create_task(Task(id="A", title="中断的推理", status="running", budget=Budget(max_requests=3, max_tokens=100000)))
    assert first.store.begin_inference("abandoned", "A", "A2", {"pool": 1}, 25000, 1)
    assert first.store.reserve_group("unattached", "A", {"pool": 1}, 1)
    first.store.close()
    second = Manager(ServiceConfig(database=path))
    try:
        await second.start()
        assert not second.store.active_inference()
        assert not second.store.allocations()
        assert second.task("A").tokens_used == 25000
        assert second.task("A").tokens_reserved == 0
        assert second.task("A").requests_used == 1
        assert second.store.begin_inference("next", "A", "A2", {"pool": 1}, 25000, 1)
        assert second.task("A").requests_used == 2
    finally:
        await second.stop()
        second.store.close()
