from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .store import Conflict


class UsageLedger:
    """真实调用累计记账；失败、未知用量和进程退出均保留保守预留。"""

    def __init__(self, path: str, limit: int = 90_000_000):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.limit = limit
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS usage_requests (
                id TEXT PRIMARY KEY, model TEXT NOT NULL, upstream_model TEXT NOT NULL,
                reserved INTEGER NOT NULL, used INTEGER, estimated INTEGER NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL);
        """)
        self.db.commit()

    def reserve(self, body: dict, upstream_model: str) -> str:
        amount = max(25000, len(json.dumps(body, ensure_ascii=False).encode()) // 2 + int(body.get("max_output_tokens", 4096)))
        request_id = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            used = self.db.execute("SELECT COALESCE(SUM(COALESCE(used,reserved)),0) FROM usage_requests").fetchone()[0]
            if used + amount > self.limit:
                raise Conflict("真实调用累计预算不足，已停止新请求")
            self.db.execute("INSERT INTO usage_requests VALUES(?,?,?,?,?,?,?,?)", (
                request_id, body.get("model", "unknown"), upstream_model, amount, None, 1, "in_progress", time.time(),
            ))
        return request_id

    def finish(self, request_id: str, usage: dict | None, state: str = "completed") -> None:
        total = None
        if usage:
            total = usage.get("total_tokens")
            if total is None and "input_tokens" in usage and "output_tokens" in usage:
                total = usage["input_tokens"] + usage["output_tokens"]
            if total is None and "prompt_tokens" in usage and "completion_tokens" in usage:
                total = usage["prompt_tokens"] + usage["completion_tokens"]
        with self.lock, self.db:
            self.db.execute("UPDATE usage_requests SET used=?,estimated=?,state=? WHERE id=?",
                            (max(0, int(total)) if total is not None else None, int(total is None), state, request_id))

    def summary(self) -> dict:
        with self.lock:
            row = self.db.execute("SELECT COUNT(*) requests,COALESCE(SUM(COALESCE(used,reserved)),0) tokens,COALESCE(SUM(estimated),0) estimated_requests FROM usage_requests").fetchone()
            return {**dict(row), "limit": self.limit, "remaining": self.limit - row["tokens"]}

    def reconcile_abandoned(self) -> None:
        """由唯一服务协调者在启动时调用；旧连接消失，用量仍按原预留计入。"""
        with self.lock, self.db:
            self.db.execute("UPDATE usage_requests SET state='interrupted_on_restart' WHERE state='in_progress'")

    def close(self):
        self.db.close()
