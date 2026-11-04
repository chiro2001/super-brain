"""专用执行端配置：重启后仍经网关，以执行端身份核对持久化任务归属。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

from .store import Conflict


def identity_token(secret: str, kind: str, identity: str) -> str:
    encoded = base64.urlsafe_b64encode(identity.encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), f"{kind}.{encoded}".encode(), hashlib.sha256).hexdigest()
    return f"{kind}.{encoded}.{signature}"


def worker_token(secret: str, host_id: str) -> str:
    return identity_token(secret, "sbw", host_id)


def worker_config(endpoint: str, token: str, model: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("执行端网关需要不含凭据、查询参数和片段的 HTTP(S) 地址")
    quote = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731
    return (
        'model_provider = "super_brain"\n'
        f'model = {quote(model)}\n'
        'check_for_update_on_startup = false\n'
        '[model_providers.super_brain]\nname = "Super Brain"\n'
        f'base_url = {quote(endpoint.rstrip("/"))}\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
        'request_max_retries = 0\nstream_max_retries = 0\n'
        '[model_providers.super_brain.http_headers]\n'
        f'X-Super-Brain-Worker-Token = {quote(token)}\n'
        '[agents]\nenabled = false\nmax_concurrent_threads_per_session = 1\n'
        '[analytics]\nenabled = false\n'
    )


def initialize_worker(home: Path, endpoint: str, secret: str, host_id: str, model: str) -> Path:
    content = worker_config(endpoint, worker_token(secret, host_id), model)
    # 只创建新目录，不覆盖个人配置、生产 daemon 或已有执行端。
    home.mkdir(mode=0o700, parents=True, exist_ok=False)
    target = home / "config.toml"
    with target.open("x", encoding="utf-8") as output:
        output.write(content)
    target.chmod(0o600)
    return target


def verify_worker_config(config: dict, endpoint: str, expected_token: str) -> str:
    provider_id = config.get("model_provider")
    providers = config.get("model_providers") or {}
    provider = providers.get(provider_id, {})
    valid = (
        set(providers) == {provider_id}
        and provider.get("base_url", "").rstrip("/") == endpoint.rstrip("/")
        and provider.get("wire_api") == "responses"
        and provider.get("requires_openai_auth") is False
        and not provider.get("supports_websockets")
        and provider.get("http_headers") == {"X-Super-Brain-Worker-Token": expected_token}
        and not any(provider.get(key) for key in ("env_http_headers", "env_key", "experimental_bearer_token", "auth", "gateway_oauth"))
        and not config.get("profiles")
        and (config.get("agents") or {}).get("enabled") is False
    )
    if not valid:
        raise Conflict("请求级预算需要专用执行端：请用 init-worker 创建默认指向管理网关的独立 CODEX_HOME")
    return provider_id
