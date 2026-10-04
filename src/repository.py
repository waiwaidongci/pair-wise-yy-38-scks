from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (BATCH_ACTIVE, BATCH_SUPERSEDED, RES_EXECUTED, RES_QUEUED,
                    RES_RELEASED, RES_RESERVED, STATES)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        # 自动提交模式，事务由 _txn 显式控制（支持 BEGIN IMMEDIATE）
        self.conn.isolation_level = None
        self._create_schema()

    @contextmanager
    def _txn(self, immediate: bool = True):
        """显式事务。immediate=True 时用 BEGIN IMMEDIATE 立即获取写锁，
        保证并发争用同一闸门时的先到者占用语义。
        若已处于事务中（由 service 层通过 txn() 开启），则不嵌套。"""
        with self._lock:
            if self.conn.in_transaction:
                yield
                return
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def txn(self, immediate: bool = True):
        """供 service 层使用的事务入口。"""
        return self._txn(immediate)

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        # executescript 会隐式提交，不能用 _txn 包裹
        self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
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
                CREATE TABLE IF NOT EXISTS gates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available','maintenance','closed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS water_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    reservoir_level REAL NOT NULL DEFAULT 0,
                    inflow REAL NOT NULL DEFAULT 0,
                    downstream_alert REAL NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    basis_observation_id INTEGER REFERENCES water_observations(id),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','superseded')),
                    evaluated_at TEXT NOT NULL,
                    created_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES capacity_batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    gate_id INTEGER NOT NULL REFERENCES gates(id),
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    requested_discharge REAL NOT NULL DEFAULT 0,
                    allocated_discharge REAL NOT NULL DEFAULT 0,
                    gap REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'reserved'
                        CHECK(status IN ('reserved','queued','executed','released')),
                    snapshot TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_no TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL,
                    request_type TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
        self._migrate_items()

    def _migrate_items(self) -> None:
        """为 items 表补充容量相关列（兼容旧库）。"""
        with self._lock:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(items)").fetchall()]
            additions = {
                "period_start": "TEXT",
                "period_end": "TEXT",
                "gate_ids": "TEXT",
                "discharge": "REAL",
                "capacity_status": "TEXT",
                "basis_observation_id": "INTEGER",
                "capacity_snapshot": "TEXT",
            }
            for name, ddl in additions.items():
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
            self.conn.commit()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        if d.get("gate_ids") is not None:
            try:
                d["gate_ids"] = json.loads(d["gate_ids"])
            except (TypeError, ValueError):
                d["gate_ids"] = []
        if d.get("capacity_snapshot") is not None:
            try:
                d["capacity_snapshot"] = json.loads(d["capacity_snapshot"])
            except (TypeError, ValueError):
                d["capacity_snapshot"] = None
        return d

    @staticmethod
    def _reservation(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        if d.get("snapshot") is not None:
            try:
                d["snapshot"] = json.loads(d["snapshot"])
            except (TypeError, ValueError):
                d["snapshot"] = None
        return d

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------

    def create_gate(self, code: str, name: str, capacity: float,
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._txn():
                cur = self.conn.execute(
                    """INSERT INTO gates(code, name, capacity, status, created_at, updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (code, name, capacity, "available", now, now),
                )
                gate_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("闸门编号已存在") from exc
        return self.get_gate(gate_id)

    def get_gate(self, gate_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
        if row is None:
            raise NotFoundError("闸门不存在")
        return dict(row)

    def get_gate_by_code(self, code: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM gates WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("闸门不存在")
        return dict(row)

    def list_gates(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM gates"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_gate(self, gate_id: int, name: Optional[str], capacity: Optional[float],
                    status: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._txn():
            gate = self.get_gate(gate_id)
            new_name = name if name is not None else gate["name"]
            new_cap = capacity if capacity is not None else gate["capacity"]
            new_status = status if status is not None else gate["status"]
            self.conn.execute(
                "UPDATE gates SET name=?, capacity=?, status=?, updated_at=? WHERE id=?",
                (new_name, new_cap, new_status, now, gate_id),
            )
        return self.get_gate(gate_id)

    # ------------------------------------------------------------------
    # 水情观测
    # ------------------------------------------------------------------

    def create_observation(self, observed_at: str, reservoir_level: float,
                            inflow: float, downstream_alert: float,
                            actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._txn():
            cur = self.conn.execute(
                """INSERT INTO water_observations(observed_at, reservoir_level, inflow,
                   downstream_alert, created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (observed_at, reservoir_level, inflow, downstream_alert, actor, now),
            )
            obs_id = int(cur.lastrowid)
        return self.get_observation(obs_id)

    def get_observation(self, obs_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM water_observations WHERE id=?", (obs_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("水情观测不存在")
        return dict(row)

    def get_latest_observation(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM water_observations ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_observations(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM water_observations ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 容量批次与预占
    # ------------------------------------------------------------------

    def create_batch(self, item_id: int, basis_observation_id: Optional[int],
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        batch_no = "BATCH-" + uuid.uuid4().hex[:12]
        with self._txn():
            cur = self.conn.execute(
                """INSERT INTO capacity_batches(batch_no, item_id, basis_observation_id,
                   status, evaluated_at, created_by) VALUES(?,?,?,?,?,?)""",
                (batch_no, item_id, basis_observation_id, BATCH_ACTIVE, now, actor),
            )
            batch_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM capacity_batches WHERE id=?", (batch_id,)
            ).fetchone()
        return dict(row)

    def get_active_batch(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM capacity_batches WHERE item_id=? AND status='active'
                   ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def supersede_active_batches(self, item_id: int) -> int:
        """将指令的所有活跃批次标记为已替代，返回受影响行数。"""
        with self._txn():
            cur = self.conn.execute(
                "UPDATE capacity_batches SET status=? WHERE item_id=? AND status='active'",
                (BATCH_SUPERSEDED, item_id),
            )
            return cur.rowcount

    def create_reservation(self, batch_id: int, item_id: int, gate_id: int,
                           period_start: str, period_end: str,
                           requested_discharge: float, allocated_discharge: float,
                           gap: float, status: str,
                           snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        now = utc_now()
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True) if snapshot else None
        with self._txn():
            cur = self.conn.execute(
                """INSERT INTO capacity_reservations(batch_id, item_id, gate_id,
                   period_start, period_end, requested_discharge, allocated_discharge,
                   gap, status, snapshot, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, item_id, gate_id, period_start, period_end,
                 requested_discharge, allocated_discharge, gap, status,
                 snapshot_json, now, now),
            )
            res_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM capacity_reservations WHERE id=?", (res_id,)
            ).fetchone()
        return self._reservation(row)

    def list_reservations(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM capacity_reservations WHERE item_id=? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [self._reservation(row) for row in rows]

    def reserved_discharge_on_gate(self, gate_id: int, period_start: str,
                                   period_end: str) -> float:
        """某闸门在指定时段内已预占（含已执行）的下泄量合计。"""
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(allocated_discharge), 0) AS s
                   FROM capacity_reservations
                   WHERE gate_id=? AND status IN (?, ?)
                     AND period_start < ? AND period_end > ?""",
                (gate_id, RES_RESERVED, RES_EXECUTED, period_end, period_start),
            ).fetchone()
        return float(row["s"])

    def release_reservations(self, item_id: int) -> int:
        """释放指令的所有预占（标记为 released），返回受影响行数。"""
        now = utc_now()
        with self._txn():
            cur = self.conn.execute(
                """UPDATE capacity_reservations SET status=?, updated_at=?
                   WHERE item_id=? AND status IN (?, ?)""",
                (RES_RELEASED, now, item_id, RES_RESERVED, RES_QUEUED),
            )
            return cur.rowcount

    def mark_reservations_executed(self, item_id: int) -> int:
        """指令执行时将预占标记为已执行。"""
        now = utc_now()
        with self._txn():
            cur = self.conn.execute(
                """UPDATE capacity_reservations SET status=?, updated_at=?
                   WHERE item_id=? AND status IN (?, ?)""",
                (RES_EXECUTED, now, item_id, RES_RESERVED, RES_QUEUED),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # 幂等键（请求号）
    # ------------------------------------------------------------------

    def get_idempotency(self, request_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM idempotency_keys WHERE request_no=?", (request_no,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["response"] = json.loads(d["response"])
        return d

    def store_idempotency(self, request_no: str, actor: str, request_type: str,
                          response: Dict[str, Any],
                          request_hash: str = "") -> None:
        now = utc_now()
        with self._txn():
            self.conn.execute(
                """INSERT INTO idempotency_keys(request_no, actor, request_type,
                   request_hash, response, created_at) VALUES(?,?,?,?,?,?)""",
                (request_no, actor, request_type, request_hash,
                 json.dumps(response, ensure_ascii=False, sort_keys=True), now),
            )

    # ------------------------------------------------------------------
    # 指令（扩展容量字段）
    # ------------------------------------------------------------------

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, *, period_start: Optional[str] = None,
                    period_end: Optional[str] = None,
                    gate_ids: Optional[List[int]] = None,
                    discharge: Optional[float] = None,
                    capacity_status: Optional[str] = None,
                    basis_observation_id: Optional[int] = None,
                    capacity_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = utc_now()
        gate_ids_json = json.dumps(gate_ids) if gate_ids is not None else None
        snapshot_json = json.dumps(capacity_snapshot, ensure_ascii=False, sort_keys=True) \
            if capacity_snapshot is not None else None
        try:
            with self._txn():
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       period_start, period_end, gate_ids, discharge, capacity_status,
                       basis_observation_id, capacity_snapshot)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, period_start, period_end,
                     gate_ids_json, discharge, capacity_status, basis_observation_id,
                     snapshot_json),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def update_item_capacity(self, item_id: int, capacity_status: str,
                             basis_observation_id: Optional[int],
                             capacity_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        now = utc_now()
        snapshot_json = json.dumps(capacity_snapshot, ensure_ascii=False, sort_keys=True) \
            if capacity_snapshot is not None else None
        with self._txn():
            self.conn.execute(
                """UPDATE items SET capacity_status=?, basis_observation_id=?,
                   capacity_snapshot=?, updated_at=? WHERE id=?""",
                (capacity_status, basis_observation_id, snapshot_json, now, item_id),
            )
        return self.get_item(item_id)

    def update_item_capacity_fields(self, item_id: int, period_start: str,
                                     period_end: str, gate_ids: List[int],
                                     discharge: float) -> Dict[str, Any]:
        """补核：更新指令绑定的时段、闸门组合与下泄量。"""
        now = utc_now()
        gate_ids_json = json.dumps(gate_ids)
        with self._txn():
            self.conn.execute(
                """UPDATE items SET period_start=?, period_end=?, gate_ids=?,
                   discharge=?, updated_at=? WHERE id=?""",
                (period_start, period_end, gate_ids_json, discharge, now, item_id),
            )
        return self.get_item(item_id)

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._txn():
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._txn():
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._txn():
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
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
