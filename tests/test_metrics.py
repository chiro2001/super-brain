from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from super_brain.manager import Manager
from super_brain.models import Backend, ServiceConfig
from super_brain.store import Store
from tests.test_api import serve_app


async def test_metrics_sampler_tracks_pool_load_throughput_and_unavailability():
    clock = [100.0]
    service = FastAPI()
    failed = [False]

    @service.get("/metrics")
    def metrics():
        clock[0] = 102
        if failed[0]:
            return PlainTextResponse("unavailable", status_code=503)
        return PlainTextResponse('vllm:num_requests_running{model="x"} 2\n'
            'vllm:num_requests_waiting 3\nvllm:kv_cache_usage_perc 0.8\n'
            'vllm:prompt_tokens_total 300\nvllm:generation_tokens_total 200\n'
            'vllm:time_to_first_token_seconds_sum 6\nvllm:time_to_first_token_seconds_count 3\n')
    async with serve_app(service) as url:
        manager = Manager(ServiceConfig(poll_interval=1), Store(":memory:"), clock=lambda: clock[0])
        manager.save("backend", Backend(id="pool", name="PD 服务", metrics_url=url + "/metrics",
            observed_at=100, metrics={"prompt_tokens_total": 100, "generation_tokens_total": 100}))
        job = asyncio.create_task(manager._metrics_loop())
        try:
            async with asyncio.timeout(5):
                while manager.store.require("backend", "pool")["health"] != "available":  # noqa: ASYNC110
                    await asyncio.sleep(.02)
            state = manager.store.require("backend", "pool")
            assert state["metrics"]["prompt_tps"] == 100 and state["metrics"]["generation_tps"] == 50
            assert state["metrics"]["ttft_mean_seconds"] == 2
            assert state["metrics"]["waiting"] == 3 and state["metrics"]["kv_cache_usage"] == .8
            failed[0] = True
            async with asyncio.timeout(5):
                while manager.store.require("backend", "pool")["health"] != "unavailable":  # noqa: ASYNC110
                    await asyncio.sleep(.02)
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            manager.store.close()
