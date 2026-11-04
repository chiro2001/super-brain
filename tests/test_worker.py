from __future__ import annotations

import copy
import tomllib

import httpx
import pytest

from super_brain.api import create_app
from super_brain.manager import Manager
from super_brain.models import (
    Budget,
    Host,
    InferenceConfig,
    ModelRoute,
    ServiceConfig,
    Session,
    Task,
)
from super_brain.store import Conflict, Store
from super_brain.worker import initialize_worker, verify_worker_config, worker_config, worker_token
from tests.support.mock_model import MockModel
from tests.test_api import serve_app


def test_worker_initialization_preserves_existing_home_and_keeps_upstream_secret_out(tmp_path):
    home = tmp_path / "独立 worker"
    path = initialize_worker(home, "http://127.0.0.1:8765/inference/v1", "admin-secret", "h", '模型"中文')
    text = path.read_text(encoding="utf-8")
    config = tomllib.loads(text)
    assert "admin-secret" not in text
    assert config["model"] == '模型"中文'
    assert verify_worker_config(config, "http://127.0.0.1:8765/inference/v1", worker_token("admin-secret", "h")) == "super_brain"
    with pytest.raises(FileExistsError):
        initialize_worker(home, "http://another/inference/v1", "other-secret", "h", "other-model")
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("change", ["upstream", "wrong_host", "task_token", "alternative_provider", "agents", "profile"])
def test_unsafe_default_worker_cannot_claim_request_budget(change):
    token = worker_token("secret", "h")
    config = tomllib.loads(worker_config("http://127.0.0.1/inference/v1", token, "test"))
    provider = config["model_providers"]["super_brain"]
    if change == "upstream":
        provider["base_url"] = "http://direct-upstream/v1"
    elif change == "wrong_host":
        provider["http_headers"]["X-Super-Brain-Worker-Token"] = worker_token("secret", "another")
    elif change == "task_token":
        provider["http_headers"]["X-Super-Brain-Task-Token"] = "persisted-task"
    elif change == "alternative_provider":
        config["model_providers"]["direct"] = copy.deepcopy(provider)
    elif change == "agents":
        config["agents"]["enabled"] = True
    else:
        config["profiles"] = {"direct": {"model_provider": "direct"}}
    with pytest.raises(Conflict, match="专用执行端"):
        verify_worker_config(config, "http://127.0.0.1/inference/v1", token)


async def test_worker_identity_requires_same_host_and_verified_managed_session(monkeypatch):
    monkeypatch.setenv("SUPER_BRAIN_TOKEN", "admin-secret")
    mock = MockModel()
    async with mock.serve() as upstream:
        config = ServiceConfig(database=":memory:", hosts=[Host(id="h", name="隔离执行端", address="localhost", enabled=False)],
            inference=InferenceConfig(enabled=True, upstream_url=upstream, ledger_path=":memory:"),
            routes=[ModelRoute(id="a", name="test", upstream_model="deepseek-flash")])
        manager = Manager(config, Store(":memory:"))
        manager.create_task(Task(id="A", title="重启仍有预算", status="running", budget=Budget(max_requests=1)))
        manager.save("session", Session(id="h:t", host_id="h", thread_id="t", task_id="A", connected=True,
            binding_source="managed", budget_accounting="gateway", budget_ready=True))
        async with serve_app(create_app(config, manager)) as url:
            # 不启动远程连接，只启用已配置身份。
            host = manager.store.require("host", "h")
            host["enabled"] = True
            manager.save("host", host)
            headers = {"X-Super-Brain-Worker-Token": manager.gateway.worker_token("h"), "thread-id": "t"}
            async with httpx.AsyncClient() as http:
                async def send(extra=None):
                    return await http.post(url + "/inference/v1/responses", headers={**headers, **(extra or {})},
                        json={"model": "test", "input": "test"})
                assert (await send()).status_code == 429
                session = manager.session("h:t")
                session.connected, session.budget_ready = True, True
                manager.save("session", session)
                for extra in [
                    {"thread-id": "unknown"}, {"session-id": "different"},
                    {"X-Super-Brain-Worker-Token": manager.gateway.worker_token("another")},
                    {"X-Super-Brain-Worker-Token": manager.gateway.token("A")},
                ]:
                    assert (await send(extra)).status_code == 429
                assert not mock.requests
                assert (await send()).status_code == 200
                assert (await send()).status_code == 429
                assert len(mock.requests) == manager.task("A").requests_used == 1
                response = await http.get(url + "/api/snapshot", headers={"Authorization": "Bearer " + manager.gateway.worker_token("h")})
                assert response.status_code == 401
        manager.store.close()
