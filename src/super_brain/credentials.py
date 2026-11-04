"""从用户明确授权的 LiteLLM 目录解析引用；不回显凭据。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(repr=False)
class LiveCredentials:
    gateway_url: str
    gateway_key: str | None = field(repr=False)
    model: str = "deepseek-flash"
    official_base: str | None = None
    official_model: str | None = None
    official_key: str | None = field(default=None, repr=False)


def from_litellm(root: Path, gateway_url: str = "http://127.0.0.1:11112/v1") -> LiveCredentials:
    env = dict(os.environ)
    file = root / ".env"
    if file.is_file():
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.removeprefix("export ").split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            env.setdefault(name.strip(), value)

    def resolve(value):
        if isinstance(value, str) and value.startswith("os.environ/"):
            return env.get(value.split("/", 1)[1])
        return value

    result = LiveCredentials(gateway_url=gateway_url, gateway_key=env.get("LITELLM_MASTER_KEY"))
    found = False
    for name in ["config.codex-gateway.yaml", "config.yaml"]:
        path = root / name
        if not path.is_file():
            continue
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result.gateway_key = resolve(config.get("general_settings", {}).get("master_key")) or result.gateway_key
        for item in config.get("model_list", []):
            if item.get("model_name") != "deepseek-flash":
                continue
            found = True
            params = item.get("litellm_params", {})
            result.official_base = resolve(params.get("api_base"))
            result.official_model = str(params.get("model", "")).removeprefix("openai/").removeprefix("deepseek/")
            result.official_key = resolve(params.get("api_key")) or env.get("DEEPSEEK_API_KEY")
    if not found:
        raise ValueError("授权目录中未找到 deepseek-flash 配置")
    return result
