from __future__ import annotations

from pathlib import Path

import yaml

from .models import ServiceConfig


def load_config(path: str | None = None) -> ServiceConfig:
    if path is None:
        return ServiceConfig()
    file = Path(path).resolve()
    config = ServiceConfig.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")) or {})
    if not Path(config.database).is_absolute() and config.database != ":memory:":
        config.database = str(file.parent / config.database)
    if config.inference.ledger_path != ":memory:" and not Path(config.inference.ledger_path).is_absolute():
        config.inference.ledger_path = str(file.parent / config.inference.ledger_path)
    return config
