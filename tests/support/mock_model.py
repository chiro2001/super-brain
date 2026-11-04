"""仅用于隔离测试的 Responses 服务，记录实际请求入口，不调用外部模型。"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class MockModel:
    def __init__(self) -> None:
        self.app = FastAPI()
        self.requests: list[dict] = []
        self.delay = 0.0
        self.text = "模拟任务完成。保留关键事实 ORBIT-42，下一步继续验证。"
        self.fail = False
        self.fail_models: set[str] = set()
        self.input_tokens = 50
        self.handler = None
        self.app.post("/v1/responses")(self.responses)
        self.app.post("/v1/responses/compact")(self.compact)
        self.app.get("/v1/models")(lambda: {"data": []})

    async def responses(self, request: Request):
        body = await request.json()
        self.requests.append({"path": request.url.path, **body})
        if self.fail or body.get("model") in self.fail_models:
            return JSONResponse({"error": {"message": "测试故障"}}, status_code=503)
        response_id = "resp_" + uuid.uuid4().hex
        item = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                "status": "completed", "phase": "final_answer",
                "content": [{"type": "output_text", "text": self.text, "annotations": []}]}
        if self.handler:
            selected = self.handler(body)
            if inspect.isawaitable(selected):
                selected = await selected
            if selected:
                item = selected
            else:
                item["content"][0]["text"] = self.text
        output_text = item.get("content", [{}])[0].get("text", "")
        response = {"id": response_id, "object": "response", "status": "completed",
                    "model": body.get("model"), "output": [item],
                    "usage": {"input_tokens": self.input_tokens, "output_tokens": 20, "total_tokens": self.input_tokens + 20,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}

        async def stream():
            sequence = 0
            if item["type"] == "function_call":
                events = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
                    {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"], "delta": item["arguments"]},
                    {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": item["id"], "arguments": item["arguments"]},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
            else:
                events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": "", "annotations": []}},
                {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                 "content_index": 0, "delta": output_text},
                {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
                 "content_index": 0, "text": output_text},
                {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0,
                 "content_index": 0, "part": item["content"][0]},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
                ]
            for event in events:
                if event["type"] == "response.output_text.delta" and self.delay:
                    await asyncio.sleep(self.delay)
                event["sequence_number"] = sequence
                sequence += 1
                yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def compact(self, request: Request):
        body = await request.json()
        self.requests.append({"path": request.url.path, **body})
        return {"id": "cmp_" + uuid.uuid4().hex, "object": "response.compaction",
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "任务摘要：ORBIT-42，继续验证。"}]}],
                "usage": {"input_tokens": 50, "output_tokens": 20}}

    @asynccontextmanager
    async def serve(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}/v1"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            sock.close()


def function_call(name: str, arguments: dict, namespace: str | None = None) -> dict:
    item = {"id": "fc_" + uuid.uuid4().hex, "type": "function_call", "name": name,
            "call_id": "call_" + uuid.uuid4().hex, "arguments": json.dumps(arguments), "status": "completed"}
    if namespace:
        item["namespace"] = namespace
    return item
