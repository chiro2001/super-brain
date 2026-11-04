from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from super_brain.inference_proxy import inference_proxy
from super_brain.store import Conflict
from super_brain.usage import UsageLedger
from tests.support.mock_model import MockModel
from tests.test_api import serve_app


def test_budget_persists_and_unknown_usage_remains_reserved(tmp_path):
    path = str(tmp_path / "usage.db")
    ledger = UsageLedger(path, limit=50000)
    first = ledger.reserve({"model": "execution"}, "deepseek-flash")
    ledger.finish(first, {"input_tokens": 1000, "output_tokens": 20})
    second = ledger.reserve({"model": "compression"}, "deepseek-flash")
    ledger.finish(second, None, "interrupted")
    assert ledger.summary()["tokens"] == 26020
    assert ledger.summary()["estimated_requests"] == 1
    with pytest.raises(Conflict):
        ledger.reserve({"model": "retry"}, "deepseek-flash")
    ledger.close()
    reopened = UsageLedger(path, limit=50000)
    assert reopened.summary()["tokens"] == 26020
    reopened.close()


def test_abandoned_live_usage_keeps_conservative_charge(tmp_path):
    path = str(tmp_path / "abandoned.db")
    first = UsageLedger(path, limit=100000)
    request_id = first.reserve({"model": "test"}, "deepseek-flash")
    first.close()
    second = UsageLedger(path, limit=100000)
    second.reconcile_abandoned()
    assert second.summary()["tokens"] == 25000 and second.summary()["requests"] == 1
    assert second.db.execute("SELECT state FROM usage_requests WHERE id=?", (request_id,)).fetchone()[0] == "interrupted_on_restart"
    second.close()


@pytest.mark.parametrize("streaming", [False, True])
async def test_client_cancel_closes_upstream_and_finalizes_once(streaming):
    upstream = FastAPI()
    received, disconnected = asyncio.Event(), asyncio.Event()
    finalized = []

    @upstream.post("/v1/responses")
    async def respond(request: Request):
        await request.body()
        received.set()
        if streaming:
            async def chunks():
                try:
                    yield 'data: {"type":"response.created"}\n\n'
                    await asyncio.sleep(20)
                finally:
                    disconnected.set()
            return StreamingResponse(chunks(), media_type="text/event-stream")
        while not await request.is_disconnected():  # noqa: ASYNC110
            await asyncio.sleep(.02)
        disconnected.set()
        return {}

    async def before(request, body):
        return "allocation"
    async def after(context, usage, state):
        finalized.append((context, state))
    ledger = UsageLedger(":memory:")
    try:
        async with serve_app(upstream) as upstream_url:
            proxy = inference_proxy(upstream_url + "/v1", None, ledger, {"test": "test"}, before=before, after=after)
            async with serve_app(proxy) as url:
                target = urlsplit(url)
                reader, writer = await asyncio.open_connection(target.hostname, target.port)
                body = json.dumps({"model": "test", "stream": streaming}).encode()
                writer.write(f"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
                await asyncio.wait_for(received.wait(), 3)
                if streaming:
                    await asyncio.wait_for(reader.readuntil(b"response.created"), 3)
                writer.close()
                await writer.wait_closed()
                await asyncio.wait_for(disconnected.wait(), 3)
                async with asyncio.timeout(3):
                    while not finalized:  # noqa: ASYNC110
                        await asyncio.sleep(.02)
                assert finalized == [("allocation", "interrupted")]
                assert ledger.summary()["requests"] == 1
                assert ledger.summary()["estimated_requests"] == 1
    finally:
        ledger.close()


async def test_inference_proxy_maps_roles_and_records_actual_usage():
    mock = MockModel()
    ledger = UsageLedger(":memory:")
    async with mock.serve() as upstream:
        app = inference_proxy(upstream, None, ledger, {"ZL-DeepSeek-v4.1": "deepseek-flash"})
        async with serve_app(app) as url:
            async with httpx.AsyncClient() as client:
                response = await client.post(url + "/v1/responses", json={"model": "ZL-DeepSeek-v4.1", "input": "测试", "stream": True})
                assert response.status_code == 200
                assert "response.completed" in response.text
                assert mock.requests[0]["model"] == "deepseek-flash"
                assert ledger.summary()["tokens"] == 70
                rejected = await client.post(url + "/v1/responses", json={"model": "unauthorized"})
                assert rejected.status_code == 403
                assert ledger.summary()["requests"] == 1
    ledger.close()
