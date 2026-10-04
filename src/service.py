from __future__ import annotations

from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, require_number, require_text,
                     require_timestamp)
from .repository import Repository
from .rules import (AUTHORIZE_ROLES, AUDIT_ROLES, CREATE_GATE_ROLES, EXECUTE_ROLES,
                    GATE_ENTITY, GATE_STATUS_ROLES, OBSERVE_ROLES, OBSERVATION_ENTITY,
                    ORDER_ENTITY, RECORD_ENTITY, SUBMIT_ROLES, VIEW_ROLES,
                    WINDOW_ENTITY, competitors_for, effective_gate_capacity,
                    normalize_allocation, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    # ---------- 闸门 ----------
    def register_gate(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_GATE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 60)
        name = require_text(payload.get("name"), "name", 120)
        design = require_number(payload.get("design_capacity"), "design_capacity",
                                0.0, strict_minimum=True)
        now = utc_now()
        with self.repository.transaction() as conn:
            gate = self.repository.create_gate(conn, code, name, design, actor, now)
            self.repository.append_audit_conn(conn, "register_gate", GATE_ENTITY,
                                              gate["id"], actor,
                                              {"code": code, "design_capacity": design})
        return gate

    def list_gates(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        with self.repository.lock:
            gates = self.repository.list_gates(self.repository.conn)
            for gate in gates:
                limit = self.repository.latest_observed_limit(self.repository.conn, gate["id"])
                gate["observed_capacity_limit"] = limit
                gate["effective_capacity"] = effective_gate_capacity(
                    gate["design_capacity"], gate["status"], limit)
        return gates

    def update_gate_status(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, GATE_STATUS_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("gate_code"), "gate_code", 60)
        status = payload.get("status")
        if status not in ('available', 'unavailable'):
            from .domain import ValidationError
            raise ValidationError("status必须是available或unavailable")
        reason = require_text(payload.get("reason", "闸门可用状态变化"), "reason", 500)
        with self.repository.transaction() as conn:
            gate = self.repository.get_gate(conn, code=code)
            updated = self.repository.set_gate_status(conn, gate["id"], status, utc_now())
            window_ids = self.repository.windows_touching_gate(conn, gate["id"])
            for window_id in window_ids:
                self._recompute_window(conn, window_id, actor,
                                       f"闸门{code}状态变为{status}：{reason}")
        return updated

    # ---------- 水情观测 ----------
    def submit_observation(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OBSERVE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("gate_code"), "gate_code", 60)
        observed_at = require_timestamp(payload.get("observed_at"), "observed_at")
        capacity_limit = require_number(payload.get("capacity_limit"), "capacity_limit", 0.0)
        source = require_text(payload.get("source", "水情观测"), "source", 200)
        with self.repository.transaction() as conn:
            gate = self.repository.get_gate(conn, code=code)
            observation = self.repository.add_observation(
                conn, gate["id"], observed_at, capacity_limit, source, actor, utc_now())
            self.repository.append_audit_conn(
                conn, "observe", OBSERVATION_ENTITY, gate["id"], actor,
                {"gate_code": code, "observed_at": observed_at,
                 "capacity_limit": capacity_limit, "source": source})
            for window_id in self.repository.windows_touching_gate(conn, gate["id"]):
                self._recompute_window(conn, window_id, actor,
                                       f"闸门{code}水情依据更新为{capacity_limit}")
        return observation

    # ---------- 容量视图 ----------
    def capacity_view(self, payload: Dict[str, Any], role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        start_at = require_timestamp(payload.get("start_at"), "start_at")
        end_at = require_timestamp(payload.get("end_at"), "end_at")
        if start_at >= end_at:
            from .domain import ValidationError
            raise ValidationError("时段开始必须早于结束")
        with self.repository.lock:
            conn = self.repository.conn
            window = conn.execute(
                "SELECT * FROM windows WHERE start_at=? AND end_at=?",
                (start_at, end_at)).fetchone()
            gates = self.repository.list_gates(conn)
            gates_by_id = {g["id"]: g for g in gates}
            used = {g["code"]: 0.0 for g in gates}
            queued_demand = {g["code"]: 0.0 for g in gates}
            basis_version = None
            if window is not None:
                basis_version = dict(window)["basis_version"]
                for order in self.repository.window_orders(conn, window["id"]):
                    if order["status"] in ('reserved', 'authorized', 'executed'):
                        for line in order["lines"]:
                            used[line["gate_code"]] = used.get(line["gate_code"], 0.0) + line["allocated"]
                    elif order["status"] == 'queued':
                        for line in order["lines"]:
                            queued_demand[line["gate_code"]] = queued_demand.get(line["gate_code"], 0.0) + line["allocated"]
            entries = []
            for gate in gates:
                limit = self.repository.latest_observed_limit(conn, gate["id"])
                capacity = effective_gate_capacity(gate["design_capacity"], gate["status"], limit)
                entries.append({
                    "gate_code": gate["code"],
                    "status": gate["status"],
                    "design_capacity": gate["design_capacity"],
                    "observed_capacity_limit": limit,
                    "effective_capacity": capacity,
                    "reserved_load": round(used.get(gate["code"], 0.0), 6),
                    "remaining": round(max(0.0, capacity - used.get(gate["code"], 0.0)), 6),
                    "queued_demand": round(queued_demand.get(gate["code"], 0.0), 6),
                })
        return {"window": {"start_at": start_at, "end_at": end_at,
                           "basis_version": basis_version, "known": window is not None},
                "gates": entries}

    # ---------- 指令提交（容量批次预占） ----------
    def submit_order(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        request_id = require_text(payload.get("request_id"), "request_id", 100)
        replay = self.repository.get_idempotent(request_id, "submit")
        if replay is not None:
            return dict(replay["payload"], replayed=True)
        purpose = require_text(payload.get("purpose"), "purpose", 200)
        discharge = require_number(payload.get("discharge"), "discharge", 0.0,
                                   strict_minimum=True)
        window_payload = payload.get("window") or {}
        start_at = require_timestamp(window_payload.get("start_at"), "window.start_at")
        end_at = require_timestamp(window_payload.get("end_at"), "window.end_at")
        if start_at >= end_at:
            from .domain import ValidationError
            raise ValidationError("时段开始必须早于结束")
        gate_codes = payload.get("gate_codes")
        if not isinstance(gate_codes, list) or not gate_codes:
            from .domain import ValidationError
            raise ValidationError("gate_codes必须是非空数组")
        gate_codes = [require_text(code, "gate_code", 60) for code in gate_codes]
        allocation = normalize_allocation(gate_codes, discharge, payload.get("allocations"))
        now = utc_now()
        with self.repository.transaction() as conn:
            # 事务内再查一次，保证写入失败/并发重试不会重复预占
            existing = self.repository.get_idempotent(request_id, "submit")
            if existing is not None:
                return dict(existing["payload"], replayed=True)
            order_row = self.repository.get_order_by_request(conn, request_id)
            if order_row is not None:
                raise ConflictError("request_id已用于其他请求")
            window = self.repository.get_or_create_window(conn, start_at, end_at, now)
            gate_map = {}
            for code in gate_codes:
                gate = self.repository.get_gate(conn, code=code)
                gate_map[code] = gate
            snapshot = self._window_snapshot(conn, window["id"])
            result = self._evaluate(allocation, snapshot)
            competitors = competitors_for(gate_codes, allocation,
                                         snapshot["orders"], window["id"])
            queued_ahead = [o for o in snapshot["orders"] if o["status"] == 'queued']
            queue_position = len(queued_ahead) + 1 if not result["filled"] else None
            status = 'reserved' if result["filled"] else 'queued'
            evaluation = {
                "basis_version": window["basis_version"],
                "window": {"start_at": start_at, "end_at": end_at},
                "reservations": result["reservations"],
                "shortfalls": result["shortfalls"],
                "competitors": competitors,
                "queue_position": queue_position,
            }
            order = self.repository.create_order(
                conn, request_id, purpose, window["id"], discharge, status,
                window["basis_version"], actor, now, evaluation)
            for code in gate_codes:
                self.repository.add_order_gate(conn, order["id"], gate_map[code]["id"],
                                               code, allocation[code])
            self.repository.append_audit_conn(
                conn, "submit", ORDER_ENTITY, order["id"], actor,
                {"request_id": request_id, "purpose": purpose, "discharge": discharge,
                 "gates": gate_codes, "status": status,
                 "basis_version": window["basis_version"],
                 "shortfalls": result["shortfalls"]})
            view = self._present_order(conn, self.repository.get_order(conn, order["id"]))
            self.repository.save_idempotent(conn, request_id, "submit", 201, view,
                                            order["id"], actor, now)
        return view

    # ---------- 总工授权 ----------
    def authorize(self, order_id: int, payload: Dict[str, Any], actor: str,
                  role: str) -> Dict[str, Any]:
        ensure_role(role, AUTHORIZE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        with self.repository.transaction() as conn:
            order = self.repository.get_order(conn, order_id)
            window = self.repository.get_window(conn, order["window_id"])
            # 历史指令没有容量依据：升级为待补核，拒绝授权（先落库留痕）
            if order["basis_version"] is None:
                if order["status"] != 'recheck_pending':
                    self.repository.update_order_status(conn, order_id, 'recheck_pending',
                                                        None, bump_version=False)
                self.repository.append_audit_conn(
                    conn, "escalate_recheck", ORDER_ENTITY, order_id, actor,
                    {"reason": "历史指令缺少容量依据，授权被拦截，维持待补核"})
        if order["basis_version"] is None:
            raise ConflictError("该指令缺少容量依据，已升级为待补核")
        with self.repository.transaction() as conn:
            order = self.repository.get_order(conn, order_id)
            window = self.repository.get_window(conn, order["window_id"])
            # 依据可能已变化：先失效重算，再判断是否仍占足容量
            if order["basis_version"] != window["basis_version"]:
                self._recompute_window(conn, window["id"], actor, "授权前依据复核")
                order = self.repository.get_order(conn, order_id)
            self._check_version(order, expected_version)
            validate_transition(order["status"], 'authorized')
            if order["status"] != 'reserved':
                raise ConflictError("只有已占足容量的指令才能授权")
            self.repository.update_order_status(conn, order_id, 'authorized',
                                                order["basis_version"])
            self.repository.append_audit_conn(
                conn, "authorize", ORDER_ENTITY, order_id, actor,
                {"basis_version": order["basis_version"],
                 "reservations": (order["evaluation"] or {}).get("reservations", [])})
            return self._present_order(conn, self.repository.get_order(conn, order_id))

    # ---------- 现场执行（写操作记录与快照） ----------
    def execute(self, order_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        ensure_role(role, EXECUTE_ROLES)
        actor = require_text(actor, "actor", 100)
        request_id = require_text(payload.get("request_id"), "request_id", 100)
        replay = self.repository.get_idempotent(request_id, "execute")
        if replay is not None:
            return dict(replay["payload"], replayed=True)
        feedback = require_text(payload.get("feedback", "闸门按授权开度执行"), "feedback", 2000)
        now = utc_now()
        with self.repository.transaction() as conn:
            existing = self.repository.get_idempotent(request_id, "execute")
            if existing is not None:
                return dict(existing["payload"], replayed=True)
            order = self.repository.get_order(conn, order_id)
            self._check_version(order, payload.get("expected_version"))
            validate_transition(order["status"], 'executed')
            if order["status"] != 'authorized':
                raise ConflictError("只有已授权指令才能执行")
            window = self.repository.get_window(conn, order["window_id"])
            lines = self.repository.order_lines(conn, order_id)
            # 已执行指令永久保留当时容量依据快照，后续重算不得改动
            gate_snapshots = []
            for line in lines:
                gate = self.repository.get_gate(conn, code=line["gate_code"])
                limit = self.repository.latest_observed_limit(conn, gate["id"])
                gate_snapshots.append({
                    "gate_code": gate["code"],
                    "allocated": line["allocated"],
                    "design_capacity": gate["design_capacity"],
                    "gate_status": gate["status"],
                    "observed_capacity_limit": limit,
                    "effective_capacity": effective_gate_capacity(
                        gate["design_capacity"], gate["status"], limit),
                })
            snapshot = {
                "request_id": order["request_id"],
                "purpose": order["purpose"],
                "discharge": order["discharge"],
                "window": {"start_at": window["start_at"], "end_at": window["end_at"]},
                "basis_version": order["basis_version"],
                "gates": gate_snapshots,
                "executed_by": actor,
                "executed_at": now,
            }
            record = self.repository.add_operation_record(
                conn, order_id, request_id, feedback, snapshot, actor, now)
            self.repository.update_order_status(conn, order_id, 'executed',
                                                order["basis_version"], executed_at=now)
            self.repository.append_audit_conn(
                conn, "execute", ORDER_ENTITY, order_id, actor,
                {"request_id": request_id, "record_id": record["id"],
                 "basis_version": order["basis_version"]})
            self.repository.save_idempotent(conn, request_id, "execute", 201,
                                            {"record": record}, order_id, actor, now)
            return {"record": record,
                    "order": self._present_order(conn, self.repository.get_order(conn, order_id))}

    # ---------- 查询 ----------
    def get_order(self, order_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        with self.repository.lock:
            order = self.repository.get_order(self.repository.conn, order_id)
            return self._present_order(self.repository.conn, order)

    def list_orders(self, role: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        with self.repository.lock:
            conn = self.repository.conn
            return [self._present_order(conn, order)
                    for order in self.repository.list_orders(conn, status)]

    def list_records(self, order_id: int, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        with self.repository.lock:
            conn = self.repository.conn
            self.repository.get_order(conn, order_id)
            return self.repository.list_operation_records(conn, order_id)

    def audit(self, role: str, entity_id: Optional[int] = None,
              entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(entity_id, entity_type)

    # ---------- 内部：容量评估与FIFO重算 ----------
    def _window_snapshot(self, conn, window_id: int) -> Dict[str, Any]:
        window = self.repository.get_window(conn, window_id)
        gates = {g["code"]: g for g in self.repository.list_gates(conn)}
        orders = self.repository.window_orders(conn, window_id)
        capacities, used = {}, {code: 0.0 for code in gates}
        for code, gate in gates.items():
            limit = self.repository.latest_observed_limit(conn, gate["id"])
            capacities[code] = effective_gate_capacity(
                gate["design_capacity"], gate["status"], limit)
        # 已执行指令按既有承诺永久占用容量；无依据历史指令不享受占用，待补核
        for order in orders:
            if order["status"] == 'executed':
                for line in order["lines"]:
                    if line["gate_code"] in used:
                        used[line["gate_code"]] += line["allocated"]
        return {"window": window, "gates": gates, "capacities": capacities,
                "used": used, "orders": orders}

    @staticmethod
    def _evaluate(allocation: Dict[str, float], snapshot: Dict[str, Any]):
        # used 只含已执行/无依据历史承诺，这里再叠加先到的活跃预占（先到先占）
        active_load = dict(snapshot["used"])
        for order in snapshot["orders"]:
            if order["status"] in ('reserved', 'authorized'):
                for line in order["lines"]:
                    active_load[line["gate_code"]] = active_load.get(line["gate_code"], 0.0) + line["allocated"]
        from .rules import evaluate_batch
        return evaluate_batch(None, list(allocation.keys()), allocation,
                              active_load, snapshot["capacities"])

    def _recompute_window(self, conn, window_id: int, actor: str, reason: str) -> None:
        """水情或闸门状态变化后：依据版本+1，未执行指令全部失效后按FIFO重算。"""
        new_basis = self.repository.bump_window_basis(conn, window_id)
        snapshot = self._window_snapshot(conn, window_id)
        capacities = snapshot["capacities"]
        # 已执行指令保留当时快照；其物理占用在新依据下按新有效容量封顶计账
        executed_load = {code: 0.0 for code in capacities}
        for order in snapshot["orders"]:
            if order["status"] == 'executed':
                for line in order["lines"]:
                    executed_load[line["gate_code"]] += min(
                        line["allocated"], capacities.get(line["gate_code"], 0.0))
        hold = executed_load
        from .rules import evaluate_batch
        queued_ids: List[int] = []
        changes = []
        for order in snapshot["orders"]:
            if order["status"] == 'executed':
                # 已执行指令保留当时快照，不参与重算
                continue
            if order["basis_version"] is None:
                # 历史无依据指令：重算也不能凭空补依据，维持待补核
                if order["status"] != 'recheck_pending':
                    self.repository.update_order_status(conn, order["id"], 'recheck_pending',
                                                        None, bump_version=False)
                    changes.append({"order_id": order["id"], "to": 'recheck_pending',
                                    "filled": False, "reason": "缺少容量依据"})
                continue
            codes = [line["gate_code"] for line in order["lines"]]
            allocation = {line["gate_code"]: line["allocated"] for line in order["lines"]}
            result = evaluate_batch(order["id"], codes, allocation, hold, capacities)
            new_status = 'reserved' if result["filled"] else 'queued'
            if not result["filled"]:
                queued_ids.append(order["id"])
            competitors = [{"order_id": o["id"], "status": o["status"]}
                           for o in snapshot["orders"]
                           if o["id"] < order["id"]
                           and any(l["gate_code"] in codes for l in o["lines"])
                           and o["status"] in ('reserved', 'authorized', 'executed')]
            queue_position = None
            if not result["filled"]:
                queue_position = sum(1 for q in queued_ids[:-1]) + 1
            evaluation = {
                "basis_version": new_basis,
                "window": {"start_at": snapshot["window"]["start_at"],
                           "end_at": snapshot["window"]["end_at"]},
                "reservations": result["reservations"],
                "shortfalls": result["shortfalls"],
                "competitors": competitors,
                "queue_position": queue_position,
                "recomputed_from": order["basis_version"],
            }
            old_status = order["status"]
            # 依据版本变化即视为失效重算，推进乐观锁版本
            self.repository.update_order_status(
                conn, order["id"], new_status, new_basis, evaluation=evaluation,
                bump_version=True)
            if old_status != new_status:
                changes.append({"order_id": order["id"], "from": old_status,
                                "to": new_status, "filled": result["filled"],
                                "shortfalls": result["shortfalls"]})
            if result["filled"]:
                for item in result["reservations"]:
                    hold[item["gate_code"]] += item["allocated"]
        self.repository.append_audit_conn(
            conn, "recompute", WINDOW_ENTITY, window_id, actor,
            {"reason": reason, "basis_version": new_basis, "changes": changes})

    def _present_order(self, conn, order: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(order)
        result["lines"] = self.repository.order_lines(conn, order["id"])
        window = self.repository.get_window(conn, order["window_id"])
        result["window"] = {"start_at": window["start_at"], "end_at": window["end_at"],
                            "basis_version": window["basis_version"]}
        return result

    @staticmethod
    def _check_version(order: Dict[str, Any], expected_version: Any) -> None:
        if expected_version is None:
            return
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        if expected_version != order["version"]:
            raise ConflictError("版本冲突，请刷新后重试")
