from __future__ import annotations

import pytest
from playwright.async_api import async_playwright, expect

from super_brain.models import Budget, Session, Task
from tests.test_api import app_and_manager, serve_app

pytestmark = pytest.mark.browser


async def test_dashboard_live_output_pause_tasks_and_verification(monkeypatch, tmp_path):
    app, manager = app_and_manager(monkeypatch)
    async with serve_app(app) as url:
        for index in range(16):
            task = Task(id=f"task-{index}", title=f"任务 {index:02d} · 接口验证", objective="完成实现并记录验证证据",
                        cwd=f"/work/project-{index}", allowed_roots=["/work"], budget=Budget(max_requests=20))
            manager.create_task(task)
            manager.save("session", Session(id=f"session-{index}", thread_id=f"thread-{index}",
                host_id="mock-host", task_id=task.id, name=f"会话 {index:02d}", model="A2-dsv41f",
                cwd=task.cwd, status="active", connected=True,
                goal={"objective": task.objective, "timeUsedSeconds": 85, "tokensUsed": 2345, "status": "active"}))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(url)
            await expect(page.get_by_role("heading", name="所有工作，一眼掌握。")).to_be_visible()
            await expect(page.locator(".session-card")).to_have_count(16)
            await expect(page.get_by_text("实时连接正常")).to_be_visible()
            await page.get_by_placeholder("搜索任务、主机或目录").fill("会话 00")
            await expect(page.locator(".session-card")).to_have_count(1)
            await page.get_by_role("button", name="暂停跟随").click()
            manager.store.event("codex.event", "session-0", {"method": "item/agentMessage/delta",
                "params": {"threadId": "thread-0", "delta": "暂停期间的有效输出"}})
            await page.wait_for_timeout(1300)
            await expect(page.locator(".terminal")).not_to_contain_text("暂停期间的有效输出")
            await page.get_by_role("button", name="继续跟随").click()
            await expect(page.locator(".terminal")).to_contain_text("暂停期间的有效输出")
            manager.store.event("codex.event", "session-0", {"method": "item/agentMessage/delta",
                "params": {"threadId": "thread-0", "delta": "<script>window.badOutput=1</script>"}})
            await expect(page.locator(".terminal")).to_contain_text("<script>")
            assert await page.evaluate("window.badOutput") is None
            await page.get_by_role("button", name="文字快照").click()
            await expect(page.locator(".report")).to_contain_text("任务 00")
            await page.get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="录入任务").click()
            await page.get_by_label("任务名称").fill("浏览器创建的真实任务记录")
            await page.get_by_label("工作目标").fill("测试任务录入与验收")
            await page.get_by_label("工作目录", exact=True).fill("/work/new")
            await page.get_by_label("允许操作的根目录（每行一个）").fill("/work")
            await page.get_by_role("button", name="保存任务").click()
            await page.get_by_role("button", name="工作任务").click()
            card = page.get_by_role("button", name="工作任务 · 优先级 0 浏览器创建的真实任务记录", exact=False)
            await card.click()
            await page.get_by_label("验收证据").fill("浏览器与 API 核对通过")
            await page.locator(".modal").get_by_role("button", name="记录验证", exact=True).click()
            await expect(page.locator(".task-column").last).to_contain_text("浏览器创建的真实任务记录")
            assert any(task["status"] == "complete" and task["verification"]["passed"] for task in manager.store.list("task"))
            manager.create_task(Task(id="parent-A", title="父任务 A"))
            manager.create_task(Task(id="child-A1", title="子任务 A1", parent_id="parent-A"))
            parent_card = page.get_by_role("button", name="工作任务 · 优先级 0 父任务 A", exact=False)
            await expect(parent_card).to_contain_text("子任务 0/1")
            manager.verify_task("child-A1", True, "人工确认子任务验证通过")
            await expect(parent_card).to_contain_text("子任务 1/1")
            await parent_card.click()
            await page.get_by_label("验收证据").fill("只属于父任务的草稿")
            await page.locator(".modal").get_by_role("button", name="子任务 A1 · 已完成").click()
            await expect(page.locator(".modal")).to_contain_text("子任务 A1")
            await expect(page.get_by_label("验收证据")).to_have_value("")
            await page.get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="会话监控").click()
            await page.get_by_placeholder("搜索任务、主机或目录").fill("")
            screenshot = tmp_path / "dashboard.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            assert not errors, errors
            await browser.close()
    manager.store.close()
