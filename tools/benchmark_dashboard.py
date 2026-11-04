"""模拟 16 会话持续事件流，测量真实网页延迟与有界内存，不调用推理模型。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import resource
import time
from pathlib import Path

from playwright.async_api import async_playwright

from super_brain.api import create_app
from super_brain.manager import Manager
from super_brain.models import ServiceConfig, Session, Task
from tests.test_api import serve_app


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    root = Path.cwd()
    run = root / ".runtime/benchmarks" / str(int(time.time()))
    run.mkdir(parents=True, exist_ok=True)
    config = ServiceConfig(database=str(run / "state.db"))
    manager = Manager(config)
    app = create_app(config, manager)
    count, latencies, heaps = 0, [], []
    started = time.perf_counter()
    cpu_before = os.times().user + os.times().system
    try:
        async with serve_app(app) as url:
            for index in range(16):
                task = Task(id=f"bench-task-{index}", title=f"模拟任务 {index:02d}", objective="持续输出测试，确认界面及时更新且缓存保持有界")
                manager.create_task(task)
                manager.save("session", Session(id=f"bench:thread-{index}", host_id="bench", thread_id=f"thread-{index}",
                    task_id=task.id, connected=True, status="active", cwd=f"/benchmark/project-{index}", model="模拟后端",
                    goal={"objective": task.objective, "timeUsedSeconds": 600, "tokensUsed": 1000, "status": "active"}))
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                await page.goto(url)
                await page.locator(".session-card").nth(15).wait_for()
                await page.get_by_role("button", name="专注监控").click()
                cdp = await page.context.new_cdp_session(page)
                benchmark_start = time.perf_counter()

                async def produce():
                    nonlocal count
                    tick = 0
                    while time.perf_counter() - benchmark_start < args.seconds:
                        with manager.store.transaction():
                            for index in range(16):
                                manager.ingest("bench", {"method": "item/agentMessage/delta", "params": {
                                    "threadId": f"thread-{index}", "itemId": f"message-{index}",
                                    "delta": f"[{tick:05d}] 会话 {index:02d} 持续推进。" + "输出与状态按事件增量记录。" * 13 + "\n"}})
                                count += 1
                        tick += 1
                        await asyncio.sleep(max(0, benchmark_start + tick * 0.2 - time.perf_counter()))

                producer = asyncio.create_task(produce())
                sample = 0
                try:
                    while not producer.done():
                        marker = f"LATENCY-SAMPLE-{sample}"
                        sent = time.perf_counter()
                        manager.ingest("bench", {"method": "item/agentMessage/delta", "params": {
                            "threadId": "thread-0", "itemId": "message-0", "delta": marker + "\n"}})
                        # 固定短间隔测 DOM 到达时间，避免 locator 退避重试把检测时间计入延迟。
                        await page.wait_for_function(
                            "marker => [...document.querySelectorAll('.terminal')].some(node => node.textContent.includes(marker))",
                            arg=marker, polling=50, timeout=5000)
                        latencies.append(time.perf_counter() - sent)
                        if sample % 6 == 0:
                            await cdp.send("HeapProfiler.collectGarbage")
                            heap = await cdp.send("Runtime.getHeapUsage")
                            heaps.append({"seconds": round(time.perf_counter()-benchmark_start,1), **heap})
                            print(f"压测 {round(time.perf_counter()-benchmark_start)} / {args.seconds} 秒，已发送 {count} 事件", flush=True)
                        sample += 1
                        await asyncio.sleep(min(5, max(0.1,args.seconds-(time.perf_counter()-benchmark_start))))
                    await producer
                    await page.screenshot(path=str(run / "dashboard.png"), full_page=True)
                    assert not errors, errors
                finally:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                    await browser.close()
        elapsed = time.perf_counter() - started
        p95 = sorted(latencies)[max(0, math.ceil(len(latencies)*0.95)-1)]
        result = {"passed": p95 <= 2, "environment": "Linux 原生服务与压测驱动同进程；真实 Chromium；模拟会话事件",
            "sessions": 16, "requested_seconds": args.seconds, "elapsed_seconds": round(elapsed,2),
            "events": count, "latency_p95_seconds": round(p95,3), "latency_max_seconds": round(max(latencies),3),
            "service_and_driver_cpu_seconds": round(os.times().user + os.times().system-cpu_before,3),
            "service_and_driver_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            "browser_heap_samples": heaps, "model_calls": 0, "browser_errors": errors}
        (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        reports = root / ".runtime/reports"
        reports.mkdir(exist_ok=True)
        (reports / "benchmark.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        assert result["passed"], "网页延迟超过目标"
    finally:
        manager.store.close()


if __name__ == "__main__":
    asyncio.run(main())
