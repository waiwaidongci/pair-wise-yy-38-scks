from __future__ import annotations

from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (CAPACITY_STATUSES, GATE_STATUSES, CapacityError,
                     ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_ACTIVE, CAP_EXECUTED, CAP_PENDING_REVIEW,
                    CAP_QUEUED, CAP_RESERVED, CREATE_ROLES, ENTITY, GATE_ENTITY,
                    GATE_MANAGE_ROLES, OBSERVATION_ENTITY, OBSERVATION_ROLES,
                    RECORD_ROLES, RES_EXECUTED, RES_QUEUED, RES_RESERVED,
                    VIEW_ROLES, allocate_status, authorization_blocker,
                    can_authorize, capacity_gap, completion_blockers,
                    escalation_required, gate_effective_capacity, is_historical,
                    needs_recalculation, priority_score, remaining_capacity,
                    response_deadline_hours, role_for_transition,
                    validate_gate_combination, validate_period,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------

    def create_gate(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, GATE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 100)
        name = require_text(payload.get("name"), "name", 200)
        capacity = require_number(payload.get("capacity", 0), "capacity", 0.0)
        gate = self.repository.create_gate(code, name, capacity, actor)
        self.repository.append_audit("create", GATE_ENTITY, gate["id"], actor, {
            "code": code, "name": name, "capacity": capacity,
        })
        return gate

    def list_gates(self, role: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._view(role)
        return self.repository.list_gates(status)

    def get_gate(self, gate_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_gate(gate_id)

    def update_gate(self, gate_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, GATE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        gate = self.repository.get_gate(gate_id)
        name = payload.get("name")
        capacity = payload.get("capacity")
        status = payload.get("status")
        if name is not None:
            name = require_text(name, "name", 200)
        if capacity is not None:
            capacity = require_number(capacity, "capacity", 0.0)
        if status is not None and status not in GATE_STATUSES:
            raise ValidationError("status不在允许范围内")
        updated = self.repository.update_gate(gate_id, name, capacity, status, actor)
        self.repository.append_audit("update", GATE_ENTITY, gate_id, actor, {
            "code": gate["code"], "from_status": gate["status"], "to_status": updated["status"],
            "from_capacity": gate["capacity"], "to_capacity": updated["capacity"],
        })
        # 闸门可用状态变化 → 未执行指令按新依据失效重算
        self._recalculate_unexecuted(actor)
        return updated

    # ------------------------------------------------------------------
    # 水情观测
    # ------------------------------------------------------------------

    def record_observation(self, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, OBSERVATION_ROLES)
        actor = require_text(actor, "actor", 100)
        observed_at = require_text(payload.get("observed_at"), "observed_at", 100)
        reservoir_level = require_number(payload.get("reservoir_level"), "reservoir_level")
        inflow = require_number(payload.get("inflow"), "inflow")
        downstream_alert = require_number(payload.get("downstream_alert"), "downstream_alert")
        obs = self.repository.create_observation(
            observed_at, reservoir_level, inflow, downstream_alert, actor)
        self.repository.append_audit("create", OBSERVATION_ENTITY, obs["id"], actor, {
            "observed_at": observed_at, "reservoir_level": reservoir_level,
            "inflow": inflow, "downstream_alert": downstream_alert,
        })
        # 水情观测变化 → 未执行指令按新依据失效重算
        self._recalculate_unexecuted(actor)
        return obs

    def list_observations(self, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        return self.repository.list_observations()

    def get_observation(self, obs_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_observation(obs_id)

    # ------------------------------------------------------------------
    # 容量评估与重算
    # ------------------------------------------------------------------

    def _evaluate_capacity(self, item: Dict[str, Any], gate_ids: List[int],
                           period_start: str, period_end: str, discharge: float,
                           actor: str) -> tuple:
        """在事务内评估容量：创建批次、逐闸门预占、更新指令容量状态。
        调用方须已处于事务中（repository.txn）。返回 (item, details)。"""
        for gid in gate_ids:
            self.repository.get_gate(gid)
        obs = self.repository.get_latest_observation()
        basis_id = obs["id"] if obs else None
        batch = self.repository.create_batch(item["id"], basis_id, actor)
        all_reserved = True
        reservations = []
        for gid in gate_ids:
            gate = self.repository.get_gate(gid)
            reserved_discharge = self.repository.reserved_discharge_on_gate(
                gid, period_start, period_end)
            remaining = remaining_capacity(gate, reserved_discharge)
            gap = capacity_gap(discharge, remaining)
            allocated = max(0.0, discharge - gap)
            status = allocate_status(gap)
            if status != RES_RESERVED:
                all_reserved = False
            snapshot = {
                "gate_id": gid,
                "gate_code": gate["code"],
                "gate_capacity": gate_effective_capacity(gate),
                "reserved_discharge": reserved_discharge,
                "remaining": remaining,
                "requested": discharge,
                "allocated": allocated,
                "gap": gap,
                "basis_observation_id": basis_id,
            }
            res = self.repository.create_reservation(
                batch["id"], item["id"], gid, period_start, period_end,
                discharge, allocated, gap, status, snapshot)
            reservations.append(res)
        cap_status = CAP_RESERVED if all_reserved else CAP_QUEUED
        item = self.repository.update_item_capacity(item["id"], cap_status, basis_id, None)
        details = {
            "batch_id": batch["id"],
            "batch_no": batch["batch_no"],
            "basis_observation_id": basis_id,
            "capacity_status": cap_status,
            "reservations": reservations,
        }
        return item, details

    def _recalculate_item_capacity(self, item: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """原子重算单条未执行指令的容量：释放旧预占、按新依据重新评估。"""
        gate_ids = item.get("gate_ids") or []
        period_start = item.get("period_start")
        period_end = item.get("period_end")
        discharge = item.get("discharge")
        with self.repository.txn():
            # 释放旧预占、替代旧批次
            self.repository.release_reservations(item["id"])
            self.repository.supersede_active_batches(item["id"])
            if not gate_ids or not period_start or not period_end or discharge is None:
                # 历史指令缺容量依据 → 待补核
                item = self.repository.update_item_capacity(
                    item["id"], CAP_PENDING_REVIEW, item.get("basis_observation_id"),
                    item.get("capacity_snapshot"))
                return {"item_id": item["id"], "capacity_status": CAP_PENDING_REVIEW,
                        "reservations": []}
            item, details = self._evaluate_capacity(
                item, gate_ids, period_start, period_end, discharge, actor)
        return {"item_id": item["id"], "capacity_status": item.get("capacity_status"),
                "details": details}

    def _recalculate_unexecuted(self, actor: str) -> List[Dict[str, Any]]:
        """水情观测或闸门状态变化后，未执行指令按新依据失效重算。
        已执行指令保留当时快照，不参与重算。"""
        items = self.repository.list_items()
        # 按提交顺序（id升序）重算，先到者优先占用容量
        items.sort(key=lambda it: it["id"])
        results = []
        for item in items:
            if not needs_recalculation(item):
                continue
            result = self._recalculate_item_capacity(item, actor)
            results.append(result)
            self.repository.append_audit("capacity_recalculated", ENTITY, item["id"], actor, {
                "capacity_status": result.get("capacity_status"),
                "basis": "water_observation_or_gate_change",
            })
        return results

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        request_no = payload.get("request_no")
        if request_no is not None:
            request_no = require_text(request_no, "request_no", 100)

        period_start = payload.get("period_start")
        period_end = payload.get("period_end")
        gate_ids = payload.get("gate_ids")
        discharge = payload.get("discharge")
        has_capacity = any(v is not None for v in (period_start, period_end, gate_ids, discharge))

        if has_capacity:
            if period_start is None or period_end is None:
                raise ValidationError("绑定容量必须提供period_start和period_end")
            period_start = require_text(period_start, "period_start", 100)
            period_end = require_text(period_end, "period_end", 100)
            validate_period(period_start, period_end)
            if gate_ids is None:
                raise ValidationError("绑定容量必须提供gate_ids")
            validate_gate_combination(gate_ids)
            if discharge is None:
                raise ValidationError("绑定容量必须提供discharge")
            discharge = require_number(discharge, "discharge", 0.000001)

        replay = None
        item = None
        details = None
        with self.repository.txn():
            # 幂等检查：凭原请求号恢复，重试不重复预占
            if request_no:
                existing = self.repository.get_idempotency(request_no)
                if existing:
                    item_id = existing["response"].get("item_id")
                    if item_id:
                        replay = self.enrich(self.repository.get_item(item_id))
                    else:
                        replay = existing["response"]
            if replay is None:
                if has_capacity:
                    item = self.repository.create_item(
                        title, description, severity, quantity, threshold,
                        external_ref, actor, capacity_status=CAP_PENDING_REVIEW,
                        period_start=period_start, period_end=period_end,
                        gate_ids=gate_ids, discharge=discharge)
                    item, details = self._evaluate_capacity(
                        item, gate_ids, period_start, period_end, discharge, actor)
                else:
                    # 历史指令无容量依据 → 升级为待补核
                    item = self.repository.create_item(
                        title, description, severity, quantity, threshold,
                        external_ref, actor)
                    item = self.repository.update_item_capacity(
                        item["id"], CAP_PENDING_REVIEW, None, None)
                if request_no:
                    self.repository.store_idempotency(
                        request_no, actor, "create_item", {"item_id": item["id"]})

        if replay is not None:
            return replay

        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "capacity_status": item.get("capacity_status"),
            "has_capacity": has_capacity,
        })
        if details:
            self.repository.append_audit("capacity_evaluated", ENTITY, item["id"], actor, {
                "batch_id": details["batch_id"], "batch_no": details["batch_no"],
                "basis_observation_id": details["basis_observation_id"],
                "capacity_status": details["capacity_status"],
                "reservations": [
                    {
                        "gate_id": r["gate_id"], "gate_code": r["snapshot"]["gate_code"],
                        "requested": r["requested_discharge"],
                        "allocated": r["allocated_discharge"], "gap": r["gap"],
                        "status": r["status"],
                    }
                    for r in details["reservations"]
                ],
            })
        return self.enrich(item)

    def supplement_capacity(self, item_id: int, payload: Dict[str, Any],
                            actor: str, role: str) -> Dict[str, Any]:
        """补核：历史指令补充容量依据后重新评估。"""
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item.get("capacity_status") != CAP_PENDING_REVIEW:
            raise ConflictError("只有待补核的历史指令才能补核")
        period_start = require_text(payload.get("period_start"), "period_start", 100)
        period_end = require_text(payload.get("period_end"), "period_end", 100)
        validate_period(period_start, period_end)
        gate_ids = payload.get("gate_ids")
        validate_gate_combination(gate_ids)
        discharge = require_number(payload.get("discharge"), "discharge", 0.000001)

        details = None
        with self.repository.txn():
            item = self.repository.update_item_capacity_fields(
                item_id, period_start, period_end, gate_ids, discharge)
            item, details = self._evaluate_capacity(
                item, gate_ids, period_start, period_end, discharge, actor)

        self.repository.append_audit("supplement", ENTITY, item_id, actor, {
            "capacity_status": item.get("capacity_status"),
            "batch_id": details["batch_id"] if details else None,
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        request_no = payload.get("request_no")
        if request_no is not None:
            request_no = require_text(request_no, "request_no", 100)

        replay = None
        record = None
        with self.repository.txn():
            # 幂等检查：重试不重复追加操作记录
            if request_no:
                existing = self.repository.get_idempotency(request_no)
                if existing:
                    replay = existing["response"]
            if replay is None:
                record = self.repository.add_record(
                    item_id, kind, detail, status, external_ref, actor)
                if request_no:
                    self.repository.store_idempotency(
                        request_no, actor, "add_record", record)

        if replay is not None:
            return replay

        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")

        # 总工授权：只能授权已占足容量的指令
        if target == "authorized":
            blocker = authorization_blocker(item.get("capacity_status"))
            if blocker:
                raise CapacityError(blocker)

        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))

        updated = self.repository.transition_item(item_id, target, expected_version, actor)

        # 执行时：保留当时快照（水情依据 + 闸门状态）
        if target == "executed":
            updated = self._snapshot_execution(updated, actor)

        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "capacity_status": updated.get("capacity_status"),
        })
        return self.enrich(updated)

    def _snapshot_execution(self, item: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """指令执行时保留当时快照：水情观测依据与各闸门状态。"""
        obs = self.repository.get_latest_observation()
        basis_id = obs["id"] if obs else None
        gates_snapshot = []
        for gid in (item.get("gate_ids") or []):
            gate = self.repository.get_gate(gid)
            gates_snapshot.append({
                "gate_id": gid,
                "gate_code": gate["code"],
                "gate_capacity": gate["capacity"],
                "gate_status": gate["status"],
            })
        snapshot = {
            "executed_at": utc_now(),
            "basis_observation_id": basis_id,
            "observation": obs,
            "gates": gates_snapshot,
        }
        with self.repository.txn():
            self.repository.mark_reservations_executed(item["id"])
            item = self.repository.update_item_capacity(
                item["id"], CAP_EXECUTED, basis_id, snapshot)
        return item

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_capacity(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        batch = self.repository.get_active_batch(item_id)
        reservations = self.repository.list_reservations(item_id)
        return {
            "item_id": item_id,
            "capacity_status": item.get("capacity_status"),
            "basis_observation_id": item.get("basis_observation_id"),
            "batch": batch,
            "reservations": reservations,
        }

    def get_gate_capacity(self, gate_id: int, period_start: str,
                          period_end: str, role: str) -> Dict[str, Any]:
        self._view(role)
        gate = self.repository.get_gate(gate_id)
        validate_period(period_start, period_end)
        reserved_discharge = self.repository.reserved_discharge_on_gate(
            gate_id, period_start, period_end)
        remaining = remaining_capacity(gate, reserved_discharge)
        return {
            "gate_id": gate_id,
            "gate_code": gate["code"],
            "gate_capacity": gate["capacity"],
            "gate_status": gate["status"],
            "period_start": period_start,
            "period_end": period_end,
            "reserved_discharge": reserved_discharge,
            "remaining_capacity": max(0.0, remaining),
        }

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["is_historical"] = is_historical(item)
        result["can_authorize"] = can_authorize(item.get("capacity_status"))
        return result
