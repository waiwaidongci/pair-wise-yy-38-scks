from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from .domain import (CAPACITY_EPSILON, GATE_STATES, ORDER_STATES,
                     UNEXECUTED_STATES, ConflictError, ValidationError)

TITLE = '汛期闸门容量批次调度'
GATE_ENTITY = '闸门'
ORDER_ENTITY = '调度指令'
WINDOW_ENTITY = '调度时段'
OBSERVATION_ENTITY = '水情观测'
RECORD_ENTITY = '操作记录'

# 已执行是终态快照；其余未执行状态都可被重算推动
TRANSITIONS = {
    'queued': ['reserved', 'recheck_pending'],
    'reserved': ['queued', 'authorized', 'recheck_pending'],
    'authorized': ['queued', 'reserved', 'recheck_pending', 'executed'],
    'recheck_pending': ['queued', 'reserved', 'recheck_pending'],
    'executed': [],
}
TRANSITION_ROLES = {
    'authorized': ['chief_engineer'],
    'executed': ['dispatcher'],
    'recheck_pending': ['chief_engineer'],
}
CREATE_GATE_ROLES = {'duty_officer'}
SUBMIT_ROLES = {'duty_officer'}
OBSERVE_ROLES = {'duty_officer', 'dispatcher'}
GATE_STATUS_ROLES = {'duty_officer'}
AUTHORIZE_ROLES = {'chief_engineer'}
EXECUTE_ROLES = {'dispatcher'}
AUDIT_ROLES = {'chief_engineer', 'viewer'}
VIEW_ROLES = {'duty_officer', 'chief_engineer', 'dispatcher', 'viewer'}


def can_transition(current, target):
    return target in TRANSITIONS.get(current, [])


def validate_transition(current, target):
    if current not in ORDER_STATES or target not in ORDER_STATES:
        raise ValidationError("未知状态")
    if not can_transition(current, target):
        raise ConflictError(f"不能从{current}转换到{target}")


def role_for_transition(target):
    return set(TRANSITION_ROLES.get(target, []))


def effective_gate_capacity(design_capacity: float, gate_status: str,
                            observed_limit) -> float:
    """依据闸门可用状态与最新水情上限求有效过流能力。"""
    if gate_status != GATE_STATES[0]:
        return 0.0
    limit = design_capacity if observed_limit is None else min(design_capacity, float(observed_limit))
    return max(0.0, limit)


def normalize_allocation(gate_codes: Sequence[str], discharge: float,
                         allocations: Mapping[str, Any] = None) -> Dict[str, float]:
    """把下泄量绑定到闸门组合；未显式分配时均分。"""
    if not gate_codes:
        raise ValidationError("至少选择一个闸门")
    if len(set(gate_codes)) != len(gate_codes):
        raise ValidationError("闸门组合不能重复")
    plan = {code: 0.0 for code in gate_codes}
    allocations = allocations or {}
    if allocations:
        unknown = [str(code) for code in allocations if code not in plan]
        if unknown:
            raise ValidationError(f"分配包含组合外闸门: {','.join(unknown)}")
        total = 0.0
        for code in gate_codes:
            value = allocations.get(code)
            if value is None:
                raise ValidationError(f"闸门{code}缺少下泄量分配")
            amount = float(value)
            if amount < 0:
                raise ValidationError(f"闸门{code}分配量不能为负")
            plan[code] = amount
            total += amount
    else:
        share = discharge / len(gate_codes)
        plan = {code: share for code in gate_codes}
        total = discharge
    if abs(total - discharge) > 1e-6:
        raise ValidationError("各闸门分配量之和必须等于指令下泄量")
    return plan


def evaluate_batch(order_id, gate_codes, allocation, hold_load: Mapping[str, float],
                   capacities: Mapping[str, float]):
    """按闸门逐一核对剩余过流能力，返回预占结果或缺口。"""
    reservations = []
    shortfalls = []
    for code in gate_codes:
        capacity = capacities.get(code, 0.0)
        used = hold_load.get(code, 0.0)
        remaining = capacity - used
        want = allocation[code]
        if want <= remaining + CAPACITY_EPSILON:
            reservations.append({
                "gate_code": code,
                "allocated": want,
                "remaining_before": max(0.0, remaining),
                "remaining_after": max(0.0, remaining - want),
                "capacity": capacity,
            })
        else:
            shortfalls.append({
                "gate_code": code,
                "allocated": want,
                "remaining": max(0.0, remaining),
                "gap": round(want - remaining, 6),
                "capacity": capacity,
            })
    return {
        "order_id": order_id,
        "filled": not shortfalls,
        "reservations": reservations,
        "shortfalls": shortfalls,
    }


def plan_window(orders: List[Any], gate_codes: Sequence[str],
                capacities: Mapping[str, float]) -> List[Dict[str, Any]]:
    """按提交先后（FIFO）重算整个时段；已执行指令作为已占容量保留快照。"""
    hold_load: Dict[str, float] = {code: 0.0 for code in gate_codes}
    results: List[Dict[str, Any]] = []
    for order in orders:
        if order["status"] == 'executed':
            for line in order.get("lines", []):
                hold_load[line["gate_code"]] = hold_load.get(line["gate_code"], 0.0) + line["allocated"]
            results.append({"order_id": order["id"], "filled": True, "frozen": True,
                            "reservations": [], "shortfalls": []})
            continue
        result = evaluate_batch(order["id"], [line["gate_code"] for line in order["lines"]],
                                {line["gate_code"]: line["allocated"] for line in order["lines"]},
                                hold_load, capacities)
        if result["filled"]:
            for item in result["reservations"]:
                hold_load[item["gate_code"]] += item["allocated"]
        results.append(result)
    return results


def competitors_for(gate_codes: Sequence[str], allocation: Mapping[str, float],
                    window_orders, window_id):
    """后到者视角：列出先到占用者、余量与缺口。"""
    conflicts = []
    for order in window_orders:
        overlap = [line for line in order.get("lines", [])
                   if line["gate_code"] in gate_codes and order["status"] in
                   ('reserved', 'authorized', 'executed')]
        if not overlap:
            continue
        conflicts.append({
            "order_id": order["id"],
            "status": order["status"],
            "gates": [line["gate_code"] for line in overlap],
            "allocated": {line["gate_code"]: line["allocated"] for line in overlap},
        })
    return conflicts
