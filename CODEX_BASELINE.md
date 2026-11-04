# Codex 版本基线与更新记录

日期：2026-11-03。范围：本机 Linux 与 gamepc Windows 的 Codex CLI 安装入口。

用户授权：更新 Windows Codex，并统一使用最新版本。

## 1. 最新稳定版确认

官方 Codex 更新日志的最新稳定版条目和 npm `@openai/codex` 的 `latest` 标签均为 `0.166.0`。查询时另有 alpha 标签，本次按稳定渠道更新，未使用预发布版。

参考：官方更新日志 `https://developers.openai.com/codex/changelog`；包版本查询使用 `npm view @openai/codex version dist-tags --json`。

## 2. 更新结果

| 环境 | 更新前 | 更新后 | 核查方式 |
| --- | --- | --- | --- |
| server-mini Linux x86_64 | `0.161.0-native.1` | `0.166.0` | 新进程执行 `codex --version`，并生成实验协议描述 |
| gamepc Windows x64，`192.168.101.5` | `0.153.2` | `0.166.0` | 新 SSH / PowerShell 7 进程执行 `codex --version`，并生成实验协议描述 |

Windows 默认入口仍为 `C:\Users\Chiro\.bun\bin\codex.exe`。内置更新器使用 `bun install -g @openai/codex`，成功安装 `0.166.0`；下载与解析约 285 秒。

Linux 原版本是带启动适配的 debug build，内置 update 命令不支持该构建。因此使用 `bun install --global @openai/codex@0.166.0` 安装正式发行版，再保留原启动适配并切换入口。

Linux 原有模型目录、代理地址和 feature 默认参数保持原值，没有修改用户的 Codex 配置文件。旧版本目录保留，未覆盖旧二进制。

Linux 默认入口：`/home/chiro/.bun/bin/codex`。

新启动适配位置：`/home/chiro/.local/share/codex-native/versions/0.166.0-upstream-b8a11fedf24b/codex`。

回退记录：`/home/chiro/.local/state/super-brain/codex-updates/20261103T132010Z`，包含原入口、原启动适配和更新结果，不包含用户 API 凭据。

新 Linux 二进制 SHA-256：`b8a11fedf24b` 是目录名使用的前缀；完整值记录在上述目录的 result.json 中。

## 3. 两端协议描述核对

分别在临时目录执行 `codex app-server generate-json-schema --experimental --out <临时目录>`，读取后删除临时目录。以下五份描述两端均存在且 SHA-256 完全一致：

| 协议描述 | SHA-256 |
| --- | --- |
| ThreadGoalGetParams.json | `1a5de89b3c5c49528af404a65411a73ef05dc7c9c49c3f00c90d9926dfe34137` |
| ThreadSettingsUpdateParams.json | `749419634bb7ed66216d72bf9d8734ecf0e6efaa8cd46fe51a117d1b42ed9657` |
| TurnSettingsUpdateParams.json | `3c0198540df66802752daa59680733221e0bc2deaf3cd0ab2c5b9daa6332ef7a` |
| ThreadCompactStartParams.json | `684b20adf20213a9fcbca31b59108feadcd5a8b693c16431c20d701049df3737` |
| ThreadLoadedListParams.json | `42c2d124ab2b2998d84f0126daae2d32b0bf9c5a7ea7266e3a0814e5e37cf00d` |

这只能证明两端安装的版本及这些协议描述一致，不能代替实际会话控制、运行时模型切换或压缩接续测试。

## 4. daemon 状态边界

更新后对默认环境执行了只读命令 `codex app-server daemon version`：

- Linux：默认控制 socket `/home/chiro/.codex/app-server-control/app-server-control.sock` 不存在，命令退出码 1。
- Windows：默认控制 socket 连接失败，命令退出码 1，系统错误码 10050。

因此本次不能确认已有工作进程所使用的 daemon 版本，也不能据此断言所有会话均未运行；它们可能使用其他配置或连接方式。

本次未重启或终止用户已有 Codex 会话与 daemon。已运行进程不会因为磁盘上 CLI 更新就自动被确认为新版本。P0 应使用独立、明确的测试配置和控制端点，核对实际服务端版本后开始测试。

## 5. 对项目计划的影响

- 新建隔离测试以 `0.166.0` 为当前统一基线。
- 原先 Windows `0.153.2` 与 Linux `0.161.0-native.1` 的记录保留为首次核查历史，不再作为实施目标版本。
- Windows 原生部署、Linux aarch64 接入、SSH 会话接管和真实模型压缩接续仍按 TEST_PLAN.md 验收。
- 未调用推理模型、修改 LiteLLM 路由、部署业务服务或自动重启现有 daemon。
