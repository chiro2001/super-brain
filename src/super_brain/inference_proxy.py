from __future__ import annotations

import asyncio
import codecs
import json
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .store import Conflict
from .usage import UsageLedger


class ClientDisconnected(Exception):
    pass


async def until_disconnected(request: Request, operation):
    """响应头尚未发出时也监听取消，及时释放上游请求。"""
    async def watch():
        while not await request.is_disconnected():  # noqa: ASYNC110 - ASGI 断开通过 receive 查询，无独立事件。
            await asyncio.sleep(0.1)

    pending = asyncio.create_task(operation)
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({pending, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done and pending not in done:
            raise ClientDisconnected
        return await pending
    finally:
        with anyio.CancelScope(shield=True):
            watcher.cancel()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(watcher, pending, return_exceptions=True)


def inference_proxy(upstream_url: str, api_key: str | None, ledger: UsageLedger,
                    aliases: dict[str, str], request_timeout: float = 180,
                    before=None, after=None, max_output_tokens: int = 4096) -> FastAPI:
    """专用受管推理入口；角色映射显式配置，凭据仅驻留进程内存。"""
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(request_timeout, connect=10)) as client:
            app.state.http = client
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "usage": ledger.summary()}

    @app.post("/v1/responses")
    @app.post("/v1/responses/compact")
    async def forward(request: Request):
        body = await request.json()
        logical_model = body.get("model")
        upstream_model = aliases.get(logical_model)
        if not upstream_model:
            return JSONResponse({"error": {"message": "模型入口未获本测试代理授权"}}, status_code=403)
        body["max_output_tokens"] = min(int(body.get("max_output_tokens") or max_output_tokens), max_output_tokens)
        context = None
        try:
            if before:
                context = await before(request, body)
            request_id = ledger.reserve(body, upstream_model)
        except Conflict as exc:
            if context and after:
                await after(context, {"input_tokens": 0, "output_tokens": 0}, "not_forwarded")
            return JSONResponse({"error": {"message": str(exc)}}, status_code=429)
        except TimeoutError:
            return JSONResponse({"error": {"message": "等待请求名额超时"}}, status_code=429)

        finalized = False
        async def finish(usage, state="completed"):
            nonlocal finalized
            if finalized:
                return
            # StreamingResponse 的取消域不能打断账本和共享名额释放。
            with anyio.CancelScope(shield=True):
                ledger.finish(request_id, usage, state)
                if context and after:
                    await after(context, usage, state)
                finalized = True
        body["model"] = upstream_model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        path = "/responses/compact" if request.url.path.endswith("/compact") else "/responses"
        client = request.app.state.http
        try:
            upstream = await until_disconnected(request, client.send(
                client.build_request("POST", upstream_url.rstrip("/") + path,
                                     headers=headers, json=body), stream=True))
        except ClientDisconnected:
            await finish(None, "interrupted")
            return JSONResponse({"error": {"message": "客户端已断开"}}, status_code=499)
        except asyncio.CancelledError:
            await finish(None, "interrupted")
            raise
        except Exception:
            await finish(None, "connection_error")
            return JSONResponse({"error": {"message": "上游连接失败，调用已计入保守用量估算"}}, status_code=502)
        if upstream.status_code >= 400:
            await upstream.aclose()
            await finish(None, f"http_{upstream.status_code}")
            return JSONResponse({"error": {"message": f"上游返回 HTTP {upstream.status_code}"}}, status_code=upstream.status_code)
        if "text/event-stream" not in upstream.headers.get("content-type", ""):
            try:
                raw = await until_disconnected(request, upstream.aread())
                value = json.loads(raw)
                usage = value.get("usage") if isinstance(value, dict) else None
            except ClientDisconnected:
                await finish(None, "interrupted")
                return JSONResponse({"error": {"message": "客户端已断开"}}, status_code=499)
            except BaseException:
                await finish(None, "interrupted")
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
            await finish(usage)
            return JSONResponse(value, headers={"X-Super-Brain-Request": request_id})

        async def stream():
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            partial, usage, state = "", None, "completed"
            try:
                async for chunk in upstream.aiter_bytes():
                    partial += decoder.decode(chunk)
                    while "\n" in partial:
                        line, partial = partial.split("\n", 1)
                        if line.startswith("data:"):
                            try:
                                event = json.loads(line[5:].strip())
                                usage = event.get("response", {}).get("usage") or event.get("usage") or usage
                            except ValueError:
                                pass
                    if len(partial) > 1024 * 1024:
                        partial = ""
                    yield chunk
            except BaseException:
                state = "interrupted"
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    await finish(usage, state)
                    await upstream.aclose()
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Super-Brain-Request": request_id})

    return app
