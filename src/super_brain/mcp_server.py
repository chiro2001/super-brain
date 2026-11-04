from __future__ import annotations

from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .manager import Manager
from .models import Host, Task, public_recovery
from .probe import probe_host

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def build_mcp(manager: Manager) -> MCPServer:
    server = MCPServer("Super Brain", version="0.1.0", instructions=(
        "维护实际工作任务和已授权 Codex 会话。先查询状态，遵守任务目录和预算。"
        "慢但有进展不压缩不迁移；只有确认长期停滞才可恢复。改变目标或范围需要用户决定。"
    ))

    @server.tool(structured_output=True, annotations=READ_ONLY)
    def work_snapshot() -> dict[str, Any]:
        """查询工作任务、会话、goal、资源及待决定事项；不会调用推理模型。"""
        return manager.snapshot()

    @server.tool(structured_output=True, annotations=READ_ONLY)
    def work_report() -> str:
        """获取与浏览器同源的快速文字工作看板。"""
        return manager.report()

    @server.tool(structured_output=True, annotations=READ_ONLY)
    async def host_discover(host_id: str) -> dict[str, Any]:
        """只读探测已注册 SSH 主机上当前账号的 Codex 控制端点和架构。"""
        return await probe_host(Host.model_validate(manager.store.require("host", host_id)))

    @server.tool(structured_output=True)
    def task_create(title: str, objective: str = "", host_id: str | None = None,
                    cwd: str | None = None, allowed_roots: list[str] | None = None,
                    dependencies: list[str] | None = None, allowed_models: list[str] | None = None,
                    budget: dict[str, Any] | None = None, acceptance: str = "",
                    parent_id: str | None = None, priority: int = 0) -> dict[str, Any]:
        """录入用户安排的任务及其明确范围、预算和验收要求。"""
        return manager.create_task(Task(title=title, objective=objective, host_id=host_id, cwd=cwd,
            allowed_roots=allowed_roots or [], dependencies=dependencies or [],
            allowed_models=allowed_models or [], budget=budget or {}, acceptance=acceptance,
            parent_id=parent_id, priority=priority))

    @server.tool(structured_output=True)
    def task_update(task_id: str, patch: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        """更新进度；改变目标、目录、模型范围或预算时返回待用户确认的提案。"""
        return manager.update_task(task_id, patch, expected=expected_revision)

    @server.tool(structured_output=True)
    def task_verify(task_id: str, passed: bool, evidence: str, evidence_event_ids: list[int] | None = None) -> dict[str, Any]:
        """提交实际验收结果与证据，失败则保留阻塞原因。"""
        return manager.verify_task(task_id, passed, evidence, event_ids=evidence_event_ids, actor="manager")

    @server.tool(structured_output=True)
    async def task_dispatch(task_id: str) -> dict[str, Any]:
        """在任务指定主机与目录内，按依赖、资源和预算分配执行。"""
        return await manager.dispatch(task_id)

    @server.tool(structured_output=True, annotations=READ_ONLY)
    def session_events(session_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """分页读取指定会话的输出和状态事件，不反复拉取整个历史。"""
        return manager.store.events(after, limit, session_id)

    @server.tool(structured_output=True)
    def session_bind(session_id: str, task_id: str, control_enabled: bool = False) -> dict[str, Any]:
        """将会话归属到已有工作任务，并按明确授权开启自动控制。"""
        return manager.bind_session(session_id, task_id, control_enabled)

    @server.tool(structured_output=True)
    async def session_message(session_id: str, text: str, operation_id: str,
                              expected_control_revision: int | None = None) -> dict[str, Any]:
        """在原任务和预算内发指令。重试同一操作必须复用 operation_id。"""
        return await manager.command(session_id, "message", {"text": text}, operation_id,
                                     expected_control_revision=expected_control_revision)

    @server.tool(structured_output=True)
    async def session_interrupt(session_id: str, expected_turn_id: str, operation_id: str,
                                expected_control_revision: int | None = None) -> dict[str, Any]:
        """中断指定活动回合；回合已变化时拒绝，避免打断新工作。"""
        return await manager.command(session_id, "interrupt", {"turn_id": expected_turn_id}, operation_id,
                                     expected_control_revision=expected_control_revision)

    @server.tool(structured_output=True)
    def session_diagnose(session_id: str, cause: str, confirmed: bool, reason: str) -> dict[str, Any]:
        """提交停滞诊断依据。只有新鲜且确认模型侧阻塞的诊断可用于迁移。"""
        return manager.diagnose(session_id, cause, confirmed, reason)

    @server.tool(structured_output=True)
    def session_progress(session_id: str, evidence: str) -> dict[str, Any]:
        """记录有证据的实际推进，取消基于旧状态的停滞诊断。"""
        return manager.mark_progress(session_id, evidence)

    @server.tool(structured_output=True)
    async def session_recover(session_id: str, target_model: str) -> dict[str, Any]:
        """仅对满足长期停滞、范围和预算条件的会话执行压缩与接续。"""
        return public_recovery(await manager.recover(session_id, target_model))

    @server.tool(structured_output=True)
    async def recovery_reconcile(recovery_id: str) -> dict[str, Any]:
        """核对中断的压缩与接续；仅在证明未发送时接续，不重发不确定指令。"""
        return public_recovery(await manager.reconcile_recovery(recovery_id))

    return server


def build_stdio_bridge(url: str, token: str | None = None) -> MCPServer:
    """stdio 入口代理到唯一管理服务，避免另开协调者或直接重开数据库。"""
    server = MCPServer("Super Brain Bridge", version="0.1.0")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def request(method: str, path: str, data=None):
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=180) as client:
            response = await client.request(method, path, json=data)
            response.raise_for_status()
            return response.json()

    @server.tool(structured_output=True, annotations=READ_ONLY)
    async def work_snapshot() -> dict[str, Any]:
        """查询现有服务的工作看板。"""
        return await request("GET", "/api/snapshot")

    @server.tool(structured_output=True, annotations=READ_ONLY)
    async def work_report() -> str:
        """获取快速文字工作看板。"""
        return (await request("GET", "/api/report"))["text"]

    @server.tool(structured_output=True)
    async def manager_action(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用受管理权限约束的工具；不授予用户确认或扩大范围的权限。"""
        return await request("POST", "/api/manager-action", {"tool": tool, "arguments": arguments})

    return server
