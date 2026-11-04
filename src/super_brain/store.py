"""单协调者 SQLite 状态、事件、控制操作与持久化预算。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock

from .models import public_recovery


class Conflict(RuntimeError):
    pass


class NotFound(KeyError):
    pass


class Store:
    def __init__(self, path: str):
        self.coordinator_lock = None
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.coordinator_lock = FileLock(str(Path(path).resolve()) + ".coordinator.lock", thread_local=False)
            self.coordinator_lock.acquire(timeout=0)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS entities (
                kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                body TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                entity_id TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS event_entity ON events(entity_id,seq);
            CREATE TABLE IF NOT EXISTS completed_items (
                session_id TEXT NOT NULL, item_id TEXT NOT NULL, seq INTEGER NOT NULL,
                PRIMARY KEY(session_id,item_id));
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
                state TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS allocations (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, backend_id TEXT NOT NULL,
                session_id TEXT, state TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS inference_requests (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, model TEXT NOT NULL,
                backend_ids TEXT NOT NULL, reserved INTEGER NOT NULL, used INTEGER,
                state TEXT NOT NULL, created REAL NOT NULL);
        """)
        self.conn.commit()

    @contextmanager
    def transaction(self):
        """可嵌套的同步事务；调用者不得跨 await 持有事务。"""
        with self.lock:
            outer = not self.conn.in_transaction
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                if outer:
                    self.conn.rollback()
                raise
            else:
                if outer:
                    self.conn.commit()

    def get(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT body FROM entities WHERE kind=? AND id=?", (kind, entity_id)).fetchone()
            return json.loads(row[0]) if row else None

    def require(self, kind: str, entity_id: str) -> dict:
        if (result := self.get(kind, entity_id)) is None:
            raise NotFound(f"{kind}/{entity_id}")
        return result

    def list(self, kind: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT body FROM entities WHERE kind=? ORDER BY updated DESC,id", (kind,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def put(self, kind: str, value: dict, expected: int | None = None) -> dict:
        value = dict(value)
        with self.transaction():
            row = self.conn.execute("SELECT revision FROM entities WHERE kind=? AND id=?", (kind, value["id"])).fetchone()
            current = row[0] if row else 0
            if expected is not None and current != expected:
                raise Conflict(f"状态已变化：期望版本 {expected}，实际 {current}")
            value["revision"] = current + 1
            value["updated_at"] = time.time()
            self.conn.execute("INSERT INTO entities VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated=excluded.updated", (
                kind, value["id"], value["revision"], json.dumps(value, ensure_ascii=False), value["updated_at"],
            ))
            self._event(f"{kind}.updated", value["id"], value)
        return value

    def _event(self, kind: str, entity_id: str, body: dict) -> int:
        if kind == "recovery.updated":
            body = public_recovery(body)
        item_id = body.get("params", {}).get("item", {}).get("id") if kind == "codex.event" and body.get("method") == "item/completed" else None
        if item_id and (existing := self.completed_item_seq(entity_id, item_id)) is not None:
            return existing
        cur = self.conn.execute("INSERT INTO events(kind,entity_id,body,created) VALUES(?,?,?,?)", (
            kind, entity_id, json.dumps(body, ensure_ascii=False), time.time(),
        ))
        seq = cur.lastrowid
        if item_id:
            self.conn.execute("INSERT INTO completed_items VALUES(?,?,?)", (entity_id, item_id, seq))
        return seq

    def completed_item_seq(self, session_id: str, item_id: str) -> int | None:
        with self.lock:
            row = self.conn.execute("SELECT seq FROM completed_items WHERE session_id=? AND item_id=?", (session_id, item_id)).fetchone()
            return row[0] if row else None

    def event(self, kind: str, entity_id: str, body: dict) -> int:
        with self.transaction():
            return self._event(kind, entity_id, body)

    def events(self, after: int = 0, limit: int = 200, entity_id: str | None = None) -> list[dict]:
        limit = max(1, min(limit, 1000))
        with self.lock:
            if entity_id:
                rows = self.conn.execute("SELECT * FROM events WHERE seq>? AND entity_id=? ORDER BY seq LIMIT ?", (after, entity_id, limit)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
            return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def recent_events(self, entity_id: str, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM events WHERE entity_id=? ORDER BY seq DESC LIMIT ?", (entity_id, min(limit, 1000))).fetchall()
            return [{**dict(row), "body": json.loads(row["body"])} for row in reversed(rows)]

    def decision_events(self, entity_id: str, limit: int = 60) -> list[dict]:
        """决策读取语义事件，避免逐字输出挤掉命令与验收证据。"""
        with self.lock:
            rows = self.conn.execute("""SELECT * FROM events WHERE entity_id=? AND kind='codex.event'
                AND json_extract(body,'$.method') IN ('item/completed','turn/completed','error',
                    'thread/settings/updated','thread/goal/updated') ORDER BY seq DESC LIMIT ?""",
                (entity_id, max(1, min(limit, 200)))).fetchall()
            return [{**dict(row), "body": json.loads(row["body"])} for row in reversed(rows)]

    def events_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        ids = list(dict.fromkeys(ids))[:100]
        with self.lock:
            rows = self.conn.execute(f"SELECT * FROM events WHERE seq IN ({','.join('?' for _ in ids)}) ORDER BY seq", ids).fetchall()
            return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def cursor(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]

    def get_operation(self, operation_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            return {**dict(row), "body": json.loads(row["body"])} if row else None

    def is_manager_message(self, session_id: str, client_id: str | None) -> bool:
        if not client_id or not client_id.startswith("sb-"):
            return False
        operation = self.get_operation(client_id[3:])
        if operation and operation["session_id"] == session_id and operation["kind"] == "message" and operation["state"] in {"sent", "completed", "uncertain"}:
            return True
        if client_id.startswith("sb-dispatch-"):
            with self.lock:
                return self.conn.execute("SELECT 1 FROM allocations WHERE session_id=? AND id LIKE ? LIMIT 1",
                    (session_id, client_id.removeprefix("sb-dispatch-") + "/%")).fetchone() is not None
        return False

    def operation(self, operation_id: str, session_id: str, kind: str, body: dict) -> dict:
        """同一个 ID 永不重复发送；连接中断的 sent 必须先核对真实状态。"""
        with self.transaction():
            row = self.conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row:
                result = {**dict(row), "body": json.loads(row["body"])}
                if result["session_id"] != session_id or result["kind"] != kind:
                    raise Conflict("操作 ID 已被其他命令使用")
                if result["body"].get("request", result["body"]) != body:
                    raise Conflict("同一个操作 ID 不能用于不同的参数")
                return result
            self.conn.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)", (
                operation_id, session_id, kind, "planned", json.dumps({"request": body}, ensure_ascii=False), time.time(),
            ))
            return {"id": operation_id, "session_id": session_id, "kind": kind, "state": "new", "body": body}

    def finish_operation(self, operation_id: str, state: str, body: dict) -> None:
        with self.transaction():
            row = self.conn.execute("SELECT body FROM operations WHERE id=?", (operation_id,)).fetchone()
            old = json.loads(row[0]) if row else {}
            value = {"request": old.get("request", {}), **body}
            self.conn.execute("UPDATE operations SET state=?,body=? WHERE id=?", (state, json.dumps(value, ensure_ascii=False), operation_id))
            self._event("operation.updated", operation_id, {"state": state, **body})

    def reserve(self, allocation_id: str, task_id: str, backend_id: str,
                backend_limit: int, task_limit: int) -> bool:
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM allocations WHERE id=?", (allocation_id,)).fetchone():
                raise Conflict("资源预留 ID 已存在")
            active = self.conn.execute("SELECT task_id,backend_id FROM allocations WHERE state='active'").fetchall()
            if sum(r["backend_id"] == backend_id for r in active) >= backend_limit:
                return False
            if sum(r["task_id"] == task_id for r in active) >= task_limit:
                return False
            self.conn.execute("INSERT INTO allocations VALUES(?,?,?,?,?,?)", (allocation_id, task_id, backend_id, None, "active", time.time()))
            return True

    def release(self, allocation_id: str) -> None:
        with self.transaction():
            self.conn.execute("UPDATE allocations SET state='released' WHERE id=?", (allocation_id,))

    def reserve_group(self, allocation_id: str, task_id: str, limits: dict[str, int], task_limit: int) -> bool:
        """落点无法固定时，为所有可能后端保守预留，单事务防止并发超发。"""
        with self.transaction():
            active = self.conn.execute("SELECT * FROM allocations WHERE state='active'").fetchall()
            task_sessions = {r["id"].split("/", 1)[0] for r in active if r["task_id"] == task_id}
            if len(task_sessions) >= task_limit:
                return False
            for key, limit in limits.items():
                if sum(r["backend_id"] == key for r in active) >= limit:
                    return False
            for key in limits:
                self.conn.execute("INSERT INTO allocations VALUES(?,?,?,?,?,?)", (
                    f"{allocation_id}/{key}", task_id, key, None, "active", time.time()))
            return True

    def attach_allocation(self, allocation_id: str, session_id: str) -> None:
        with self.transaction():
            self.conn.execute("UPDATE allocations SET session_id=? WHERE id LIKE ?", (session_id, allocation_id + "/%"))

    def reserve_recovery(self, allocation_id: str, task_id: str, session_id: str,
                         limits: dict[str, int], task_limit: int) -> bool:
        with self.transaction():
            # 同一会话换入口，不重复消耗它自己的任务名额；其他会话仍正常计数。
            others = [row for row in self.allocations() if row["session_id"] != session_id]
            if len({row["id"].split("/", 1)[0] for row in others if row["task_id"] == task_id}) >= task_limit:
                return False
            if any(sum(row["backend_id"] == key for row in others) >= limit for key, limit in limits.items()):
                return False
            for key in limits:
                self.conn.execute("INSERT INTO allocations VALUES(?,?,?,?,?,?)", (
                    f"{allocation_id}/{key}", task_id, key, session_id, "active", time.time()))
            return True

    def commit_recovery_allocation(self, session_id: str, allocation_id: str) -> None:
        with self.transaction():
            self.conn.execute("UPDATE allocations SET state='released' WHERE session_id=? AND id NOT LIKE ?",
                              (session_id, allocation_id + "/%"))

    def release_group(self, allocation_id: str) -> None:
        with self.transaction():
            self.conn.execute("UPDATE allocations SET state='released' WHERE id LIKE ?", (allocation_id + "/%",))

    def release_task(self, task_id: str) -> None:
        with self.transaction():
            self.conn.execute("UPDATE allocations SET state='released' WHERE task_id=?", (task_id,))

    def begin_inference(self, request_id: str, task_id: str, model: str,
                        limits: dict[str, int], reserved: int, global_limit: int = 2) -> bool:
        with self.transaction():
            task = self.require("task", task_id)
            budget = task["budget"]
            if task["status"] in {"complete", "failed", "paused", "blocked"}:
                raise Conflict("任务状态不允许继续推理")
            if budget.get("max_requests") is not None and task["requests_used"] >= budget["max_requests"]:
                raise Conflict("任务请求预算已耗尽")
            if budget.get("max_tokens") is not None and task["tokens_used"] + task.get("tokens_reserved", 0) + reserved > budget["max_tokens"]:
                raise Conflict("任务剩余 token 预算不足以预留本次请求")
            active = self.conn.execute("SELECT backend_ids FROM inference_requests WHERE state='in_progress'").fetchall()
            if len(active) >= global_limit:
                return False
            counts: dict[str, int] = {}
            for row in active:
                for key in json.loads(row[0]):
                    counts[key] = counts.get(key, 0) + 1
            if any(counts.get(key, 0) >= limit for key, limit in limits.items()):
                return False
            self.conn.execute("INSERT INTO inference_requests VALUES(?,?,?,?,?,?,?,?)", (
                request_id, task_id, model, json.dumps(list(limits)), reserved, None, "in_progress", time.time()))
            task["requests_used"] += 1
            task["tokens_reserved"] = task.get("tokens_reserved", 0) + reserved
            self._update_task_in_transaction(task)
            self._event("inference.started", task_id, {"request_id": request_id, "model": model, "capacity_reservations": list(limits)})
            return True

    def _update_task_in_transaction(self, task: dict) -> None:
        task["revision"] += 1
        task["updated_at"] = time.time()
        self.conn.execute("UPDATE entities SET revision=?,body=?,updated=? WHERE kind='task' AND id=?", (
            task["revision"], json.dumps(task, ensure_ascii=False), task["updated_at"], task["id"]))
        self._event("task.updated", task["id"], task)

    def finish_inference(self, request_id: str, used: int | None, state: str) -> None:
        with self.transaction():
            row = self.conn.execute("SELECT * FROM inference_requests WHERE id=?", (request_id,)).fetchone()
            if not row or row["state"] != "in_progress":
                return
            charged = row["reserved"] if used is None else max(0, used)
            task = self.require("task", row["task_id"])
            task["tokens_used"] += charged
            task["tokens_reserved"] = max(0, task.get("tokens_reserved", 0) - row["reserved"])
            self._update_task_in_transaction(task)
            self.conn.execute("UPDATE inference_requests SET used=?,state=? WHERE id=?", (charged, state, request_id))
            self._event("inference.completed", task["id"], {"request_id": request_id, "tokens": charged, "estimated": used is None, "state": state})

    def active_inference(self) -> list[dict]:
        with self.lock:
            return [{**dict(row), "backend_ids": json.loads(row["backend_ids"])} for row in self.conn.execute("SELECT * FROM inference_requests WHERE state='in_progress'")]

    def charge_observed_usage(self, task_id: str, tokens: int) -> None:
        with self.transaction():
            task = self.require("task", task_id)
            task["requests_used"] += 1
            task["tokens_used"] += max(0, tokens)
            self._update_task_in_transaction(task)
            self._event("inference.observed", task_id, {"tokens": tokens, "source": "native_estimate", "hard_request_cap": False})

    def allocations(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM allocations WHERE state='active'")]

    def close(self) -> None:
        with self.lock:
            self.conn.close()
            if self.coordinator_lock:
                self.coordinator_lock.release()
