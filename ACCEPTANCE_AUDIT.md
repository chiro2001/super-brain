# 首版完成标准核对

日期：2026-11-04。用户已明确确认“首版验收限定为 deepseek-flash”，对应范围已写入 REQUIREMENTS.md、PLAN.md、TEST_PLAN.md。下表按更新后的十项标准核对；生产 A2/A3/ZL、真实 vLLM metrics、PD / KV 效果和自研专用唤醒适配保留为后续工作。

| PLAN.md 首版完成标准 | 权威证据 | 判定 |
| --- | --- | --- |
| 1. 任务贯穿分配、执行、打断、恢复、验收和汇报 | workflow.json 联合验证 A/B/C/D 树、紧急优先、两次打断、原会话接续、依赖和父任务验收；live-managed-recovery.json、live-repair.json 为真实模型交付 | 通过，分别标注模拟源与真实模型 |
| 2. 已有活动会话发现、监听和控制，版本范围明确 | test_codex_protocol 的第二客户端与活动回合权威读取；README 限定 daemon / 账号 / 版本 | 通过已测试环境，不承诺任意旧版 |
| 3. 平铺、文字、goal 与资源关联一致 | test_dashboard、test_api、Windows 原生 Edge / MCP 报告、benchmark.json | 通过；16 会话、24,000 事件，p95 1.597 秒，普通监控模型调用 0 |
| 4. 正常慢不迁移，仅确认长期停滞后主动恢复 | test_core 停滞矩阵、test_control 人工冲突、test_manager_recovery、原生自动与用户压缩专项 | 通过；压缩门槛只限制管理器主动恢复，生产阈值需按任务校准 |
| 5. 真实 deepseek-flash 压缩后继续原任务并产出正确结果 | live-protocol.json 保留 ORBIT-42；live-managed-recovery.json 实际生成 code=ORBIT-42、sum=42，输入字节不变，任务验收 complete | 通过；逻辑角色均映射到 deepseek-flash，未调用生产 ZL |
| 6. 主会话及子代理预算验证，区分软硬限制 | gateway / usage / worker 测试，agent-budget.json、daemon-restart-budget-*.json、windows-managed.json、live-managed-worker.json | 通过；真实 worker 重启后同一任务两次请求计账、第三次拒绝；token 为保守估算与 usage 结算 |
| 7. Linux、Windows 原生、aarch64 实测 | linux-final.xml 108 项、windows-final.xml 83 项、windows-native.json、windows-managed.json、aarch64.json | 通过；Windows 原生服务、Codex MCP、Edge 和 SSH 反向网关均有实测 |
| 8. 自动诊断、修复、有限重试和超范围询问 | live-repair.json；test_supervisor、test_api 的权限提案及重试上限 | 通过真实 deepseek-flash 修复闭环及隔离授权流程 |
| 9. 关键故障恢复、明确分类与未解决项 | recovery-restart-*.json、service-process-restart.json、test_reconnection、test_reconciliation、test_probe | 通过；历史不足时保留待核对，不盲目重发 |
| 10. 安装、配置、MCP、skill、启动和恢复可复现 | README、两份示例配置、部署工具、release.json、wheel 独立安装冒烟、skill 校验、Windows 原生消费者 | 通过；首版配置全部角色共用配额池，只路由 deepseek-flash |

首版必需验收项、Linux / Windows 首版配置和发布包核对均已通过，本项目 goal 按用户确认范围完成；后续未验证能力不记为已完成。机器可读汇总为 .runtime/reports/acceptance-summary.json。

## 真实调用与范围

统一账本 .runtime/live-usage.db 累计 **44 请求、564,377 tokens**，含 3 次缺失 usage 的保守估算。停止线 90M，总授权 <100M。新增真实 worker 重启测试为 2 请求、13,462 tokens，任务账与总账一致。没有调用生产 A2/A3/ZL，没有重启用户生产 daemon，没有把凭据写入报告或部署包。

## 后续边界

- 接入实际 A2/A3/ZL 路由、可达 vLLM metrics 和已知共享关系后，单独校准生产阈值并验证 PD / KV 效果。当前模型入口切换不代表已更换物理实例，也不提供 KV cache 直接搬运。
- 请求硬预算要求专用 worker 默认连接管理网关；旧个人环境只提供原生用量估算。token 估算不保证 tokenizer 级精确硬上限。
- 断线只能补齐 daemon 持久化的完成项，无法重建所有瞬时逐字事件；发送状态不确定时保留待核对。
- 自研 agent 已有标准 MCP 契约，专用自动唤醒适配需获得其接口后再联调。
