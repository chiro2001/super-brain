from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .api import create_app
from .config import load_config
from .mcp_server import build_stdio_bridge
from .worker import initialize_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Super Brain 工作任务与 Codex 管理")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动常驻服务、看板和 HTTP MCP")
    serve.add_argument("--config")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    mcp = sub.add_parser("mcp", help="通过 stdio 代理到现有管理服务")
    mcp.add_argument("--url", default="http://127.0.0.1:8765")
    mcp.add_argument("--token-env", default="SUPER_BRAIN_TOKEN")
    worker = sub.add_parser("init-worker", help="创建重启后仍受预算约束的独立 CODEX_HOME")
    worker.add_argument("--home", required=True)
    worker.add_argument("--url", required=True, help="执行端可访问的地址，以 /inference/v1 结尾")
    worker.add_argument("--host-id", required=True, help="与服务配置中的主机 ID 一致")
    worker.add_argument("--model", required=True)
    worker.add_argument("--token-env", default="SUPER_BRAIN_TOKEN")
    args = parser.parse_args()
    if args.command == "init-worker":
        secret = os.environ.get(args.token_env)
        if not secret:
            parser.error(f"请通过 {args.token_env} 提供管理令牌，用于生成执行端身份")
        try:
            path = initialize_worker(Path(args.home).expanduser().resolve(), args.url, secret, args.host_id, args.model)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"已创建专用执行端配置：{path}；启动 Codex 时设置 CODEX_HOME={path.parent}")
    elif args.command == "mcp":
        build_stdio_bridge(args.url, os.environ.get(args.token_env)).run("stdio")
    else:
        config = load_config(args.config)
        config.listen = args.host or config.listen
        config.port = args.port or config.port
        if config.listen not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get(config.token_env):
            parser.error(f"监听非本机地址时请设置 {config.token_env}，保护会话控制入口")
        uvicorn.run(create_app(config), host=config.listen, port=config.port, workers=1)


if __name__ == "__main__":
    main()
