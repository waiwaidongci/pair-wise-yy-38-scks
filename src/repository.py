from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ORDER_STATES, GATE_STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    @contextmanager
    def transaction(self):
        """全局串行化写事务：立即加锁，提交点唯一，保证并发先到先占。"""
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @property
    def lock(self):
        return self._lock

    def _create_schema(self) -> None:
        order_statuses = ",".join("'" + s + "'" for s in ORDER_STATES)
        gate_statuses = ",".join("'" + s + "'" for s in GATE_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS gates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    design_capacity REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ({gate_statuses})),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    basis_version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(start_at, end_at)
                );
                CREATE TABLE IF NOT EXISTS observation_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id INTEGER NOT NULL REFERENCES gates(id) ON DELETE CASCADE,
                    observed_at TEXT NOT NULL,
                    capacity_limit REAL NOT NULL,
                    source TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_obs_gate_time
                    ON observation_limits(gate_id, observed_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    purpose TEXT NOT NULL,
                    window_id INTEGER NOT NULL REFERENCES windows(id),
                    discharge REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({order_statuses})),
                    basis_version INTEGER,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    executed_at TEXT,
                    evaluation TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_orders_window ON orders(window_id, id);
                CREATE TABLE IF NOT EXISTS order_gates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    gate_id INTEGER NOT NULL REFERENCES gates(id),
                    gate_code TEXT NOT NULL,
                    allocated REAL NOT NULL,
                    UNIQUE(order_id, gate_id)
                );
                CREATE TABLE IF NOT EXISTS operation_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id),
                    request_id TEXT NOT NULL UNIQUE,
                    detail TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotent_requests (
                    request_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    http_status INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    order_id INTEGER,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    # ---------- 闸门 ----------
    def create_gate(self, conn, code: str, name: str, design_capacity: float,
                    actor: str, now: str) -> Dict[str, Any]:
        cur = conn.execute(
            """INSERT INTO gates(code, name, design_capacity, status, created_at)
               VALUES(?,?,?, 'available', ?)""",
            (code, name, design_capacity, now))
        return self.get_gate(conn, int(cur.lastrowid))

    def get_gate(self, conn, gate_id: int = None, code: str = None) -> Dict[str, Any]:
        if code is not None:
            row = conn.execute("SELECT * FROM gates WHERE code=?", (code,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
        if row is None:
            raise NotFoundError("闸门不存在")
        return dict(row)

    def list_gates(self, conn) -> List[Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def set_gate_status(self, conn, gate_id: int, status: str, now: str) -> Dict[str, Any]:
        conn.execute("UPDATE gates SET status=? WHERE id=?", (status, gate_id))
        return self.get_gate(conn, gate_id)

    def latest_observed_limit(self, conn, gate_id: int):
        row = conn.execute(
            """SELECT capacity_limit FROM observation_limits
               WHERE gate_id=? ORDER BY observed_at DESC, id DESC LIMIT 1""",
            (gate_id,)).fetchone()
        return None if row is None else float(row["capacity_limit"])

    def add_observation(self, conn, gate_id: int, observed_at: str, capacity_limit: float,
                        source: str, actor: str, now: str) -> Dict[str, Any]:
        cur = conn.execute(
            """INSERT INTO observation_limits(gate_id, observed_at, capacity_limit,
               source, actor, created_at) VALUES(?,?,?,?,?,?)""",
            (gate_id, observed_at, capacity_limit, source, actor, now))
        obs_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM observation_limits WHERE id=?", (obs_id,)).fetchone()
        return dict(row)

    # ---------- 时段 ----------
    def get_or_create_window(self, conn, start_at: str, end_at: str, now: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM windows WHERE start_at=? AND end_at=?",
                           (start_at, end_at)).fetchone()
        if row is not None:
            return dict(row)
        cur = conn.execute(
            "INSERT INTO windows(start_at, end_at, basis_version) VALUES(?,?,1)",
            (start_at, end_at))
        return dict(conn.execute("SELECT * FROM windows WHERE id=?",
                                (int(cur.lastrowid),)).fetchone())

    def get_window(self, conn, window_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("时段不存在")
        return dict(row)

    def bump_window_basis(self, conn, window_id: int) -> int:
        conn.execute("UPDATE windows SET basis_version=basis_version+1 WHERE id=?",
                     (window_id,))
        return int(self.get_window(conn, window_id)["basis_version"])

    def list_windows(self, conn) -> List[Dict[str, Any]]:
        return [dict(row) for row in conn.execute("SELECT * FROM windows ORDER BY id").fetchall()]

    # ---------- 指令 ----------
    def create_order(self, conn, request_id: str, purpose: str, window_id: int,
                     discharge: float, status: str, basis_version: int, actor: str,
                     now: str, evaluation: dict = None) -> Dict[str, Any]:
        cur = conn.execute(
            """INSERT INTO orders(request_id, purpose, window_id, discharge, status,
               basis_version, version, created_by, created_at, updated_at, evaluation)
               VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
            (request_id, purpose, window_id, discharge, status, basis_version,
             actor, now, now,
             json.dumps(evaluation, ensure_ascii=False, sort_keys=True) if evaluation else None))
        return self.get_order(conn, int(cur.lastrowid))

    def get_order(self, conn, order_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("调度指令不存在")
        order = dict(row)
        order["evaluation"] = json.loads(order["evaluation"]) if order.get("evaluation") else None
        return order

    def get_order_by_request(self, conn, request_id: str) -> Optional[Dict[str, Any]]:
        row = conn.execute("SELECT * FROM orders WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        order = dict(row)
        order["evaluation"] = json.loads(order["evaluation"]) if order.get("evaluation") else None
        return order

    def update_order_status(self, conn, order_id: int, status: str, basis_version,
                            executed_at: Optional[str] = None,
                            evaluation: Optional[dict] = None,
                            bump_version: bool = True) -> None:
        version_sql = "version=version+1, " if bump_version else ""
        conn.execute(
            f"""UPDATE orders SET status=?, basis_version=?, {version_sql}
               updated_at=?, executed_at=COALESCE(?, executed_at), evaluation=COALESCE(?, evaluation)
               WHERE id=?""",
            (status, basis_version, utc_now(), executed_at,
             json.dumps(evaluation, ensure_ascii=False, sort_keys=True) if evaluation else None,
             order_id))

    def add_order_gate(self, conn, order_id: int, gate_id: int, gate_code: str,
                       allocated: float) -> None:
        conn.execute(
            """INSERT INTO order_gates(order_id, gate_id, gate_code, allocated)
               VALUES(?,?,?,?)""", (order_id, gate_id, gate_code, allocated))

    def order_lines(self, conn, order_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM order_gates WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
        return [dict(row) for row in rows]

    def window_orders(self, conn, window_id: int) -> List[Dict[str, Any]]:
        orders = [dict(row) for row in conn.execute(
            "SELECT * FROM orders WHERE window_id=? ORDER BY id", (window_id,)).fetchall()]
        for order in orders:
            order["evaluation"] = json.loads(order["evaluation"]) if order.get("evaluation") else None
            order["lines"] = self.order_lines(conn, order["id"])
        return orders

    def list_orders(self, conn, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = conn.execute("SELECT * FROM orders WHERE status=? ORDER BY id DESC",
                                (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
        orders = []
        for row in rows:
            order = dict(row)
            order["evaluation"] = json.loads(order["evaluation"]) if order.get("evaluation") else None
            orders.append(order)
        return orders

    def windows_touching_gate(self, conn, gate_id: int) -> List[int]:
        rows = conn.execute(
            """SELECT DISTINCT o.window_id FROM order_gates og
               JOIN orders o ON o.id = og.order_id
               WHERE og.gate_id=?""",
            (gate_id,)).fetchall()
        return [int(row["window_id"]) for row in rows]

    # ---------- 操作记录 ----------
    def add_operation_record(self, conn, order_id: int, request_id: str, detail: str,
                             snapshot: dict, actor: str, now: str) -> Dict[str, Any]:
        cur = conn.execute(
            """INSERT INTO operation_records(order_id, request_id, detail, snapshot,
               actor, created_at) VALUES(?,?,?,?,?,?)""",
            (order_id, request_id, detail,
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now))
        record_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM operation_records WHERE id=?",
                           (record_id,)).fetchone()
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        return result

    def list_operation_records(self, conn, order_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM operation_records WHERE order_id=? ORDER BY id",
            (order_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            result.append(item)
        return result

    # ---------- 幂等 ----------
    def save_idempotent(self, conn, request_id: str, scope: str, http_status: int,
                        payload: dict, order_id: Optional[int], actor: str,
                        now: str) -> None:
        try:
            conn.execute(
                """INSERT INTO idempotent_requests(request_id, scope, http_status, payload,
                   order_id, actor, created_at) VALUES(?,?,?,?,?,?,?)""",
                (request_id, scope, http_status,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 order_id, actor, now))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("request_id已被占用") from exc

    def get_idempotent(self, request_id: str, scope: Optional[str] = None):
        with self._lock:
            if scope:
                row = self.conn.execute(
                    "SELECT * FROM idempotent_requests WHERE request_id=? AND scope=?",
                    (request_id, scope)).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM idempotent_requests WHERE request_id=?",
                    (request_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ---------- 审计 ----------
    def append_audit_conn(self, conn, action: str, entity_type: str, entity_id: int,
                          actor: str, detail: dict) -> None:
        row = conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]))

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        clauses, params = [], []
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
