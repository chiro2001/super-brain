# Super Brain

通过 SSH 集中管理 Linux 上的 Codex daemon，维护工作任务、预算与验收证据。Linux 或 Windows 原生服务向浏览器和管理 agent 提供同一份状态；普通监听、规则与看板刷新不调用模型。

需求基线见 [REQUIREMENTS.md](REQUIREMENTS.md)，实测结果与边界见 [TEST_RESULTS.md](TEST_RESULTS.md)，交付进度见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。2026-11-04 用户确认首版验收限定 deepseek-flash；真实 A2/A3/ZL、PD / KV 效果与生产 vLLM metrics 接入留待后续。

## 安装与启动

需要 Python 3.12+、uv、系统 OpenSSH。被管理的 Linux 端需要兼容的 Codex daemon；本项目实测版本为 `0.166.0`。已有独立 stdio 进程没有可连接控制端点时，不能直接完整接管。管理服务与执行端必须分别配置 SSH 密钥、known_hosts 和跳板路径。

从 GitHub 源码检出后，首次启动前需要 Node.js 构建网页：先运行 `npm --prefix frontend ci` 和 `npm --prefix frontend run build`。网页构建产物不提交到 Git；已构建的 wheel 包含网页，安装 wheel 不需要 Node.js。

Linux：

```sh
uv sync --frozen --group dev
cp config.deepseek-flash.example.yaml config.local.yaml
uv run super-brain serve --config config.local.yaml
```

Windows 使用 PowerShell 7，在 `D:\Projects\super-brain`：

```powershell
uv sync --frozen --group dev --link-mode copy
Copy-Item config.deepseek-flash.example.yaml config.local.yaml
uv run super-brain serve --config config.local.yaml
```

默认网页为 `http://127.0.0.1:8765`，HTTP MCP 为同地址的 `/mcp`。默认不连接任何主机；在网页添加已授权 SSH 主机，或填写配置。服务只读发现并展示已有会话；关联任务、开启自动管理和停滞恢复分别授权。

首版示例启用受管推理网关，启动前设置 `SUPER_BRAIN_TOKEN` 与 `SUPER_BRAIN_UPSTREAM_KEY`，按实际环境修改 `inference.upstream_url` 和执行端可达地址，再配置专用 worker。所有角色最终只调用 deepseek-flash，并共用一个明确配置的逻辑配额池；示例的 `available` 表示准入开启，不表示已测得物理 vLLM 健康。`ZL-DeepSeek-v4.1` 在这个示例里仅是恢复协议的压缩角色名，显式映射到 deepseek-flash，不调用生产 ZL。原生产拓扑参考保留在 `config.example.yaml`。

项目已构建的 wheel 包含网页，安装后不需要 Node。修改前端源码时执行：

```sh
cd frontend
npm ci
npm run build
```

同一个 SQLite 数据库只允许一个协调进程。stdio MCP 应连接现有服务，不能另开一个协调者读取同库。停止服务用 Ctrl+C；保留数据库与推理账本后重启。数据库与推理账本的相对路径以配置文件所在目录解析。

监听内网地址时必须设置 `SUPER_BRAIN_TOKEN`，并把实际服务主机添加到 `allowed_hosts`。网页登录使用该令牌；MCP 使用 Bearer 环境变量。密钥放环境变量，不写入版本库、任务文本或日志。跨不可信网络可使用 SSH 隧道或提供 TLS 的反向代理。

## SSH 与会话

配置示例：

```yaml
hosts:
  - id: work-arm
    name: ARM 工作机
    address: 已配置的SSH别名
    codex_command: [/usr/local/node/bin/codex]
    allowed_roots: [/home/user/projects]
    # 可选：明确指定独立环境，避免发现错误 daemon。
    codex_home: /home/user/.codex
    control_socket: /home/user/.codex/app-server-control/app-server-control.sock
```

`address` 可使用系统 SSH 别名；也可独立填写 `username`、`port`、`jump`、`identity_file`、`known_hosts_file`。省略控制端点时，仅探测该账号的进程和已知 Codex 目录，不扫描整机或内网。

Linux 支持 SSH stdio 代理和 TCP 到 Unix socket 转发。Windows 默认使用后者。`transport: stdio` 或 `tunnel` 可显式指定。项目已实测 localhost 两种路径，以及 Windows → Linux 和经 Windows 跳板 → aarch64。

断线后重新订阅并读取权威状态，按完成项 ID 补齐历史。已持久化的逐字输出可以分页查看；断线期间未被 daemon 历史保留的逐字时间、瞬时错误和中间状态无法完整重建。看板会标记观测中断。

## 工作任务与看板

先录入工作任务 A/B/C，再用“所属工作任务”和“前置任务”维护 A1/A2 等子任务。每项任务指定主机、Linux 工作目录、允许根目录、模型范围、预算和验收要求。

父任务展示子任务验收进度，详情可直接打开子任务。管理 agent 可以在所有所属子任务都验收通过后，引用它们的实际执行证据验收父任务；仍需核对父任务自己的验收要求。目标或范围改变、增加子任务或复查失败时，相关旧验收会撤销，避免已完成状态掩盖新增工作。

“开始执行”创建受管会话；也可以在已有会话详情中关联任务并选择自动管理。运行前核验目录的远端真实路径，避免符号链接越界。前置任务未验收通过时不会下发后续任务。完成工作需要实际验收证据，不能以会话空闲代替。

会话监控支持平铺、搜索、专注模式、暂停跟随和历史分页。goal 时长与 tokens 使用 daemon 上报值，不把估算当成原生计时。任务详情可以查看依赖、用量、子代理名额，启动执行、调整预算或记录人工验收。会话详情可追加指令、中断指定回合及设置停滞阈值。人工指令优先，会取消与之冲突的恢复。

任务预算修改会影响之后的请求准入；已有会话的原生子代理并发设置不会被悄悄重写。需要不同名额时新建相应受管会话。

## MCP 与 skill

支持 Streamable HTTP 的 Codex 配置示例：

```toml
[mcp_servers.super_brain]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "SUPER_BRAIN_TOKEN"
default_tools_approval_mode = "writes"
```

服务声明了只读查询的工具注解。管理写操作的客户端确认策略由你按已授权范围配置；服务仍会核验目录、预算、依赖、会话控制权和操作幂等性。

需要 stdio 时使用桥接：

```toml
[mcp_servers.super_brain]
command = "uv"
args = ["--directory", "/实际路径/super-brain", "run", "--frozen", "super-brain", "mcp", "--url", "http://127.0.0.1:8765"]
env_vars = ["SUPER_BRAIN_TOKEN"]
```

Windows 的 `--directory` 改为 `D:\\Projects\\super-brain`。桥接只暴露 `work_snapshot`、`work_report` 和 `manager_action`；其余 HTTP MCP 工具通过 `manager_action` 的 `tool` 与 `arguments` 调用。自研 agent 可直接使用标准 MCP SDK。

管理 skill 位于 `.agents/skills/super-brain-manager`。在本项目运行的 agent 可以发现它；其他工作区可把该文件夹复制到其 Codex 环境的 `skills` 目录，然后请求：

> 使用 $super-brain-manager 给我工作看板，处理原任务和预算内的错误；需要扩大范围时提出变更。

常用工具：

| 用途 | 工具 |
| --- | --- |
| 快速文字看板、完整快照 | `work_report`、`work_snapshot` |
| 工作任务与依赖 | `task_create`、`task_update`、`task_dispatch`、`task_verify` |
| 端点探测、会话归属 | `host_discover`、`session_bind` |
| 增量输出与控制 | `session_events`、`session_message`、`session_interrupt` |
| 进展、诊断与恢复 | `session_progress`、`session_diagnose`、`session_recover`、`recovery_reconcile` |

自动控制携带快照的 `expected_control_revision`。同一操作重试复用 `operation_id`。结果不确定时核对实际会话，不能换 ID 重发。`task_update` 改目标、验收要求、目录或预算会产生提案，只有网页的人工作用接口可以批准。

`task_create` 可直接指定 `parent_id` 和 `priority`。启用 `auto_dispatch` 后，已就绪任务按优先级等待共享资源，依赖未完成时不会启动。等待依赖或容量时不会重复通过 SSH 核查目录；正式执行前仍核验远端真实路径。同一任务的并发分配会串行核对，不创建重复的执行会话。

## 推理预算与共享后端

路由与后端可扩展。多个入口共享实例时，使用同一个 `backend_id`；一个入口有多个可能落点且不能固定时，为全部候选保守预留。ZL 后端保持未知，不计算确定新增容量。`can_pin_backend` 只是能力声明；本版没有实际固定落点适配，要求固定的分配会明确拒绝。

后端可配置 `metrics_url` 和 `metrics_token_env`。支持运行请求、队列、KV 使用、抢占、token 计数与 TTFT，连续采样可计算吞吐。指标过期或不可用时不分配新工作。只填写已确认的服务/资源池，PD 组件不能重复算作独立容量。

已有会话的原生用量属于观测估算；它们可能绕过管理服务，不能承诺请求级硬限制。新建受管会话可启用专用推理网关：

```yaml
inference:
  enabled: true
  upstream_url: http://127.0.0.1:11112/v1
  api_key_env: SUPER_BRAIN_UPSTREAM_KEY
  public_url: http://可从执行端访问的管理服务:8765
  ledger_path: .runtime/inference-usage.db
  total_token_limit: 90000000
  max_output_tokens: 16384
  max_concurrent_requests: 2
  queue_timeout: 600
```

同时设置管理令牌。执行端无法直连管理服务时，可为 host 指定 `gateway_remote_port`，由 SSH 反向转发执行端的回环端口；也可显式指定 `inference_url`。转发远端端口应专用且未占用。

请求级预算还要求专用 `CODEX_HOME`。在管理机生成配置模板，例如：

```bash
super-brain init-worker --home .runtime/worker-a2-template \
  --host-id server-a2 --model A2-dsv41f \
  --url http://127.0.0.1:18876/inference/v1
```

此命令读取 `SUPER_BRAIN_TOKEN`，只创建新目录；已有目录会被拒绝。将模板的 `config.toml` 通过 SSH 复制到执行端新建的专用目录，用该目录作为 `CODEX_HOME` 启动 daemon，并在 host 配置中填写其 socket。上述回环地址对应 `gateway_remote_port: 18876`；直连部署则填写执行端可达的网关地址。管理服务须先启动。Windows 管理机同样提供 `init-worker` 命令。

模板只含受限的执行端签名，不含管理令牌、上游 key 或固定任务身份。默认 provider 始终指向管理网关，默认关闭子代理；每个任务按额度启用能力。分配前会核对 daemon 的默认配置，存在直连上游的默认配置时拒绝把新任务作为请求硬预算任务启动。现有个人环境仍可用于观测和原生用量估算。

执行端 daemon 重启后，管理器会主动恢复已授权且仍在运行的已知任务，沿用原 provider ID，不设置新的模型、目录或任务输入。若独立客户端已经先恢复，Codex 0.166.0 会忽略再次 resume 的 provider 覆盖；此时网关用执行端签名与原生 `thread-id`，查回持久化任务归属并继续计账。未登记、未核对、跨执行端或预算耗尽的请求均不发往上游。控制连接建立与预算核对完成分别显示，核对前不自动下发工作。

普通工作不因本机制换模型或压缩。原生客户端抢先恢复时，丢失的子代理启用覆盖会回落到默认关闭；管理器恢复尚未加载的会话时会重新注入任务配额。这是受信任执行账号下的受管运行方式，不阻止该账号主动改配置、另带上游凭据启动其他进程。

受管请求携带签名任务身份，主会话与原生子代理共用预算，按请求原子预留共享后端名额。任务的请求数与并发准入受硬限制；tokens 根据请求体保守预留，并用实际上报 usage 结算。模型重试也算请求；未知 usage 保留估算。token 估算不能证明绝对不超真实 tokenizer 数值，预算应留余量。客户端取消会关闭上游连接并释放并发名额，未知消耗仍计账。

## 停滞恢复与低成本自动处理

默认停滞恢复关闭。只对已授权会话显式设置阈值：例如诊断 900 秒、确认 1800 秒、冷却 1800 秒；实际值需按任务阶段校准。仍有正常输出、有效进展、正常长工具执行、等待人工或观测断线时均不迁移。负载与 KV 仅帮助分配新工作，不能单独触发恢复。长期只吐字而没有产物时可以唤醒一次诊断；这不会自动解除“持续输出不迁移”的限制。

这个长期停滞门槛只限制管理器主动恢复。用户在原生客户端自行切换压缩入口并压缩、Codex 自身自动压缩均照常，并继续计入同一受管任务预算。活动压缩期间不重复恢复或自动追加工作；压缩项完成后开始下一阶段的停滞计时，不把压缩本身记为实际交付进展。未加载、状态未知、用量受限或系统错误的会话不会仅因耗时被迁移。

确认长期模型侧阻塞后，服务记录恢复事务，停止对应回合，选择 ZL，压缩并等待压缩回合正常退出，再切换执行入口、接续原任务并验证新进展。接续指令有持久化 outbox，避免断线后重复执行。

接续时重新附上任务系统保存的原始目标、目录、模型范围、预算、用量与验收要求，减少压缩摘要遗漏约束的影响。回合基线保留在数据库，只向网页和管理 agent 提供其数量，不把大段内部 ID 填入管理上下文。

恢复中服务退出或控制结果不确定时，会保留 `reconcile_required`。重连后自动读取权威历史，核对恢复前的回合基线、任务范围、goal、压缩结果和接续消息 ID。确认压缩完成且接续尚未发送时，在原预算内发送一次；已发送的接续根据实际回合与工具结果结算，不重复发送。断线期间出现其他输入或目标变化时取消旧恢复。可以通过 `recovery_reconcile` 主动重新核对；证据缺失或发送结果仍不确定时保留待核对，不盲目重发。它不提供跨实例 KV cache 搬运，也不保证压缩立刻释放旧后端全部缓存。

服务重启会释放旧 HTTP 请求占用的共享并发，未知消耗继续按预留计账，不退还已经发送的请求数。事件接收缓冲溢出时重建控制订阅并补齐完成项，不只显示一条告警。

复杂诊断与修复需要配置决策驱动器，未配置时看板显示待处理。Codex 驱动器示例：

```yaml
supervisor:
  kind: codex
  host_id: 已连接的管理Codex主机ID
  cwd: /home/user/projects/manager
  model: deepseek-flash
  model_provider: 与该daemon配置一致的provider标识
  max_invocations: 20
  structured_output: false
```

决策器使用单独的只读会话，读取完成项与错误证据，输出结构化决策，由服务核验执行。默认用 JSON 提示与本地 schema 校验；上游确认支持严格 JSON Schema 后可开启 `structured_output`。调用预算单独限制，配置的 provider 费用仍应在上游或专用账本限制。`kind: webhook` 可接入自研 agent 的 HTTP JSON 决策接口，字段见 `Decision` schema；专用客户端尚未提供时，只承诺标准 MCP 契约。

## 验证与交付

```sh
uv run ruff check src tests tools
uv run pytest -m 'not live' -q
uv build
uv run python -m tools.benchmark_dashboard --seconds 300
uv run python -m tools.test_aarch64 --host 已授权SSH别名
uv run python -m tools.deploy_windows --install
uv run python -m tools.test_windows_bridge
uv run python -m tools.test_windows_managed
```

浏览器测试需要先安装 Playwright Chromium。Windows 平台脚本使用原生 Edge。SSH localhost 测试使用 `.runtime/test-ssh/known_hosts`，缺少该文件时对应测试会跳过；正式验收不能把跳过记为通过。

真实模型测试只在 `SUPER_BRAIN_LIVE=1` 时启用，凭据在内存中从获授权 LiteLLM 目录读取，账本为 `.runtime/live-usage.db`，90M 停止线。测试通过不代表所有生产路由已验收，详见测试报告。
