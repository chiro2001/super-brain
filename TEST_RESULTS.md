# 验证结果与验收边界

更新日期：2026-11-04。按用户最新确认，首版验收限定 deepseek-flash。下文分别记录模拟源、真实 Codex / SSH、平台与真实模型证据；生产 A2/A3/ZL、PD / KV 效果及真实 vLLM metrics 留作后续，不阻塞本次首版。

## 测试结果

| 验证 | 结果 | 证据与边界 |
| --- | --- | --- |
| Linux 核心、API、MCP、SSH、Codex、浏览器回归 | 108 项通过 | .runtime/reports/linux-final.xml；pytest -m 'not live'。这里的 108 是测试函数实例数，不等同于 52 个计划场景均使用生产模型；分层覆盖见下表 |
| 任务、依赖、目录、权限提案与持久化 | 通过 | tests/test_core.py；防循环依赖、远端符号链接越界、配置更新和指标过期检查 |
| 多目标、多阶段、两次打断与紧急插队 | 通过 | tests/integration/test_workflow.py、.runtime/reports/workflow.json；实际 Codex + SSH + 受管网关，A1 原会话两次打断后接续，紧急 B1 优先，A2/A3 依赖正确，五个子任务产物验收，A/B/D 父任务交付，C 保留待办；压缩 0 次 |
| 父任务验收与目标变化 | 通过 | tests/test_task_tree.py；父子关系防环，所有子任务通过后引用真实证据验收父任务，目标变化撤销旧验收并拒绝重用旧事件 ID |
| 人工优先、幂等、重启与恢复冲突 | 通过 | tests/test_control.py；长压缩让路、过期指令拒绝、预算耗尽仍可停止、不确定指令重启后不重发 |
| 恢复中断后的自动核对与接续 | 通过 | tests/integration/test_recovery_restart.py、tests/test_reconciliation.py；原生 Codex、SQLite 重开、SSH 自动重连、丢失回复与人工介入 |
| 管理服务真实进程强制退出 | 通过 | .runtime/reports/service-process-restart.json；第一 PID 强制终止，第二 PID 重新连接相同 daemon，压缩仅一次，自动接续并完成实际工具工作 |
| 活动会话第二客户端接入、goal、入口切换 | 通过 | tests/integration/test_codex_protocol.py，真实 Codex 0.166.0、模拟 Responses 服务 |
| 管理器完整停滞恢复与新进展 | 通过 | test_manager_recovery.py、.runtime/reports/manager-recovery.json；模拟时钟、真实 Codex，实际执行成功工具命令。目标容量保守预留，接续后释放旧预留 |
| 压缩 / 目标入口失败 | 通过 | 原生 Codex + 模拟 HTTP 503；保留失败状态、恢复原设置或记录接续失败，未循环压缩 |
| SSH localhost stdio、TCP 转发与断线重连 | 通过 | test_codex_protocol.py、test_reconnection.py；掉线时标记观测中断，重连补齐完成项，无额外推理请求 |
| 中文、空格、引号和特殊字符目录 | 通过 | 实际 SSH 参数往返，并拒绝目录内部符号链接逃逸；Windows 原生分配也经过实际远端目录核查 |
| 单次探测完成与失败识别 | 通过 | test_probe.py；程序打印中间 JSON 后失败不算成功，完成帧避免 Windows OpenSSH 管道 EOF 挂起 |
| 指标采集与不可用状态 | 通过，模拟指标源 | tests/test_metrics.py：真实 HTTP，排队、KV、吞吐、TTFT 与 503；未连接生产 vLLM |
| 受管请求预算、并发、未知 usage | 通过 | tests/test_gateway.py、test_usage.py；响应头等待与流式阶段取消均关闭上游，结算一次，释放名额 |
| 执行端 daemon 重启后的预算保护 | Linux 与 Windows 原生通过 | tests/integration/test_managed_dispatch.py；分别由管理器或独立客户端先恢复，同一任务继续计账，未核对和预算耗尽时不调用上游；专用 CODEX_HOME 默认指向网关 |
| Windows 原生网关与执行端重启 | 通过 | tools.test_windows_managed、windows-managed.json；原生 Windows 网关/模拟上游，经 SSH 反向转发连接 Linux worker，daemon 重启后同一任务两个请求计账，第三次拒绝 |
| 用户原生压缩入口与计账 | 通过 | native-user-compaction.json；未启用管理器停滞恢复，用户主动 A2 → ZL 压缩 → A3，三个请求均计入任务 |
| 原生子代理配额及任务身份继承 | 通过 | test_agent_budget.py、.runtime/reports/agent-budget.json；名额 1，实际仅一个子代理，父子共 4 请求计入同一任务 |
| 原生 Codex 管理决策器 | 通过 | test_manager_recovery.py；实际 Codex 解析 JSON 决策并向原任务下发修复，模型为模拟源 |
| 浏览器平铺、输出、任务和人工验收 | 通过 | tests/browser/test_dashboard.py；16 会话、暂停跟随、日志转义、文字快照、任务录入、验收 |
| Windows 原生核心 / API / MCP 回归 | 通过 | gamepc，原生 Python 3.12.10；83 项通过，最新回归结果保存在 .runtime/reports/windows-final.xml |
| Windows → Linux Codex、原生 Codex MCP 消费者 | 通过 | .runtime/reports/windows-native.json；独立 CODEX_HOME 加载 skill 并实际调用 work_snapshot，原生服务通过 SSH 控制 Linux 会话 |
| Windows 原生 Edge | 通过 | .runtime/reports/windows-dashboard.png；没有用 WSL 代替 |
| Linux aarch64 与跨网段跳板 | 通过 | .runtime/reports/aarch64.json；a3-21，架构 aarch64，独立 Codex 0.166.0，A2 → ZL → A3 请求均为模拟，不消耗 GPU |
| 真实 deepseek-flash 压缩接续 | 通过 | .runtime/reports/live-protocol.json；执行、压缩和接续共 3 请求、29,493 tokens，保留 ORBIT-42；逻辑 ZL 映射到获授权 deepseek-flash |
| 真实失败 → 决策 → 代码修复 → 验收 | 通过 | .runtime/reports/live-repair.json；原会话修复 clamp 上边界，测试文件字节未变，unittest 通过，管理器引用成功工具事件自动验收。本次成功闭环 74,413 tokens |
| 真实管理器停滞恢复与文件交付 | 通过 | .runtime/reports/live-managed-recovery.json；仅 deepseek-flash，代理故障和模拟时钟，Manager.recover 完成压缩与换入口，生成正确的 code / sum JSON，输入字节不变。本次成功闭环 82,728 tokens |
| 真实受管 worker 重启与预算 | 通过 | live-managed-worker.json；首版配置、实际 SSH，独立客户端先恢复后仍按原任务计账，两次真实请求共 13,462 tokens，阶段事实保留，第三次请求被拒绝，总账与任务账一致 |
| 管理 skill 与发布包 | 通过 | skill-creator 校验；ruff check；npm run build；uv build，wheel 包含 HTML / JS / CSS |

## 性能

证据：.runtime/reports/benchmark.json。真实 Chromium，Linux 服务与压测驱动同进程，16 个模拟会话、每会话每秒 5 事件，运行 300 秒，共 24,000 事件。

- 浏览器 DOM 到达延迟：p95 **1.597 秒**，最大 1.680 秒。
- 服务与驱动合计 CPU 4.65 秒，峰值 RSS 99,676,160 bytes。该数不是纯服务独占指标，浏览器进程另计。
- 输出缓存达到上限后，浏览器 embedder heap 约 72～79 MB，JS heap 约 8～12 MB，未随总历史持续线性增长。
- 普通监听与刷新模型调用 **0**，浏览器错误 0。
- 原先 locator 的退避重试让检测结果约 3 秒；改用 50ms 固定间隔观察 DOM 后重新执行完整五分钟压测，以上为重新实测结果，未放宽 2 秒目标。

## 真实模型记账

统一账本 .runtime/live-usage.db：**44 次请求，564,377 tokens**，含 3 次缺失 usage 的保守估算，停止线 90,000,000。授权总量 <100M。计费次数按 API 请求，不把一次 Codex turn 当成一次请求。

真实修复联调暴露并修复了两项问题：部分上游拒绝严格 outputSchema，现默认 JSON 提示＋本地 schema 校验，能力确认后才开启严格模式；逐字事件会挤掉验收工具结果，现单独读取语义完成项并按 seq 查验原始证据。

真实文件任务的首次联调暴露字段名偏差，验收未通过；接续改为重新附上任务系统的原始要求，并明确测试的 JSON 字段后，重跑通过。失败与成功请求均包含在总账。内部回合基线不进入网页快照、SSE 或 MCP 结果，避免长会话把管理 agent 的上下文填满。

凭据未写入源码、报告、部署包或网页。生产 ZL、A2/A3 推理入口未被调用；不能将获授权模型的角色映射称为生产 ZL 已通过。

## 与测试计划对应的覆盖

| 计划场景 | 当前覆盖 |
| --- | --- |
| TASK-01～04、AUTH-01～03 | 分项及实际 Codex 联合流程通过：A/B/C/D 任务树、紧急插队、共享请求排队、两次打断、原会话接续、依赖推进及父任务验收。生产参数仍需试运行校准 |
| CODEX-01～06、SSH-01～05 | 活动会话、goal、控制、子代理、SSH 通道、掉线、跳板与特殊目录通过；断线完成项补齐有证据，未保留瞬时事件不可重建 |
| RES-01～04、BUDGET-01～02 | 别名共享、未知容量不计、动态健康 / 过期、原子准入、父子预算与取消结算通过；真实资源拓扑待生产配置 |
| STALL-01～05 | 慢 / 高负载不迁移；等待 / 工具 / 断线不误判；停滞需诊断；重复输出可去重诊断，不直接迁移。规则、模拟时钟与原生恢复通过 |
| STALL-06 | 原生自动阈值专项通过；隔离会话触发 contextCompaction，仍用原执行入口，无管理器恢复 |
| REC-01～04、REC-06 | 模拟源的原生压缩接续、失败、设置时机、人工取消通过；真实 deepseek-flash 协议接续通过 |
| REC-05 | 实际进程被强制终止后自动核对压缩并接续；回复丢失时凭消息 ID 核对已发送工作，不重复发送；人工输入取消旧恢复。证据仍不充分时保留待核对 |
| REC-07 | 按首版更新后的标准通过：真实 deepseek-flash 压缩保留 ORBIT-42，Manager.recover 接续后生成正确文件并验证；生产 ZL 未执行，列作后续 |
| UI-01～04、MCP-01～03 | 网页、文字、标准 SDK、Windows Codex＋skill 通过；自研专用客户端未提供，只承诺标准 MCP 契约 |
| SUP-01～02 | 模拟 / 原生 / 真实模型决策修复均通过，未配置驱动时显示待处理 |
| PLATFORM-01～04、DURABLE-01～02 | Linux、Windows 原生、真实 aarch64、持久化与有界窗口通过；只保证已测试的环境与接口 |

## 已知限制与待生产验收

1. 首版真实入口限定 deepseek-flash。A2/A3/ZL 原定义、真实 metrics 和物理共享关系未验证；后续接入时再提供配置，不阻塞当前首版。
2. 用户已明确选择首版验收仅用 deepseek-flash，没有新增生产 ZL 调用授权；逻辑压缩角色不代表调用了生产 ZL。
3. 恢复中断后自动按回合基线、范围、goal 和消息 ID 核对；仅在证明可以接续时自动继续。缺少历史或发送状态仍不确定时保留 reconcile_required，不以无条件重发掩盖分布式控制的不确定性。
4. token 预留是保守估算，未知 tokenizer / usage 时不能保证精确硬上限。请求数、共享并发与新会话子代理名额已实测；跨 daemon 重启的请求限制要求专用 worker 配置。原生客户端抢先恢复时默认关闭子代理，不增加配额；旧个人环境的直接 provider 请求只能观测估算。
5. 测试仅操作专用目录、配置、socket、数据库与进程，没有重启生产 daemon。aarch64 的旧 daemon 不因独立测试通过而自动获得新版兼容结论。

首版完成标准已按用户确认范围更新。对应验收证据齐全，最终判定见 ACCEPTANCE_AUDIT.md；生产未执行项继续保留为后续未验证。

补充探测：授权配置还有 deeepseek-v4-flash-vllm → 本机 8888 端口的兼容入口；其 /metrics 在当前管理机连接失败。证据 configured-vllm-metrics.json。未把该探测推断为 A2/A3 的真实健康状态，也没有调用其推理接口。
