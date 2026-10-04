from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (CAPACITY_STATUSES, GATE_STATUSES, RESERVATION_STATUSES,
                     ConflictError, ValidationError)

TITLE = '水库防汛调度与操作确认'
ENTITY = '调度指令'
ID_PREFIX = 'RF'
GATE_ENTITY = '闸门'
OBSERVATION_ENTITY = '水情观测'
BATCH_ENTITY = '容量批次'

SEVERITIES = ['routine', 'attention', 'urgent', 'emergency']
STATES = ['draft', 'checked', 'authorized', 'executed', 'closed']
TRANSITIONS = {
    'draft': ['checked'],
    'checked': ['authorized'],
    'authorized': ['executed'],
    'executed': ['closed'],
    'closed': [],
}
TRANSITION_ROLES = {
    'checked': ['duty_officer'],
    'authorized': ['chief_engineer'],
    'executed': ['dispatcher'],
    'closed': ['chief_engineer'],
}
CREATE_ROLES = set(['duty_officer'])
RECORD_ROLES = set(['duty_officer', 'dispatcher'])
AUDIT_ROLES = set(['chief_engineer', 'viewer'])
VIEW_ROLES = set(['duty_officer', 'chief_engineer', 'dispatcher', 'viewer'])
GATE_MANAGE_ROLES = set(['duty_officer', 'chief_engineer'])
OBSERVATION_ROLES = set(['duty_officer', 'dispatcher'])

SEVERITY_WEIGHT = {
    'routine': 1.0,
    'attention': 3.0,
    'urgent': 6.0,
    'emergency': 9.0,
}
DEADLINE_HOURS = {
    'routine': 72,
    'attention': 24,
    'urgent': 8,
    'emergency': 4,
}
TERMINAL_STATES = set(['closed'])

# 容量状态常量
CAP_RESERVED = 'reserved'
CAP_QUEUED = 'queued'
CAP_PENDING_REVIEW = 'pending_review'
CAP_EXECUTED = 'executed'

# 预占状态常量
RES_RESERVED = 'reserved'
RES_QUEUED = 'queued'
RES_EXECUTED = 'executed'
RES_RELEASED = 'released'

# 闸门状态常量
GATE_AVAILABLE = 'available'
GATE_MAINTENANCE = 'maintenance'
GATE_CLOSED = 'closed'

# 批次状态常量
BATCH_ACTIVE = 'active'
BATCH_SUPERSEDED = 'superseded'

# 未执行指令状态（需要参与容量重算）
UNEXECUTED_STATES = set(['draft', 'checked', 'authorized'])
# 已执行指令状态（保留快照，不再重算）
EXECUTED_STATES = set(['executed', 'closed'])


def priority_score(severity, quantity=0.0, threshold=1.0, open_records=0):
    if severity not in SEVERITY_WEIGHT:
        raise ValidationError("unknown severity")
    ratio = quantity / threshold if threshold > 0 else 1.0
    return max(0, min(10, int(round(
        SEVERITY_WEIGHT[severity]
        + min(4.0, ratio * 4.0)
        + min(3.0, float(open_records))
    ))))


def response_deadline_hours(severity, quantity=0.0, threshold=1.0):
    if severity not in DEADLINE_HOURS:
        raise ValidationError("unknown severity")
    ratio = quantity / threshold if threshold > 0 else 1.0
    return max(1, int(DEADLINE_HOURS[severity] / max(1.0, ratio)))


def escalation_required(severity, quantity=0.0, threshold=1.0):
    return severity == SEVERITIES[-1] or (threshold > 0 and quantity >= threshold)


def can_transition(current, target):
    return target in TRANSITIONS.get(current, [])


def validate_transition(current, target):
    if current not in STATES or target not in STATES:
        raise ValidationError("未知状态")
    if not can_transition(current, target):
        raise ConflictError(f"不能从{current}转换到{target}")


def completion_blockers(target, open_records):
    return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records > 0 else []


def role_for_transition(target):
    return set(TRANSITION_ROLES.get(target, []))


# ---------------------------------------------------------------------------
# 容量规则
# ---------------------------------------------------------------------------

def validate_period(period_start: str, period_end: str) -> None:
    """校验时段起止格式与先后关系。"""
    if not isinstance(period_start, str) or not period_start.strip():
        raise ValidationError("period_start不能为空")
    if not isinstance(period_end, str) or not period_end.strip():
        raise ValidationError("period_end不能为空")
    if period_start >= period_end:
        raise ValidationError("period_start必须早于period_end")


def validate_gate_combination(gate_ids: List[int]) -> None:
    """校验闸门组合非空且闸门存在。"""
    if not isinstance(gate_ids, list) or len(gate_ids) == 0:
        raise ValidationError("闸门组合不能为空")
    if len(set(gate_ids)) != len(gate_ids):
        raise ValidationError("闸门组合中存在重复闸门")
    for gid in gate_ids:
        if not isinstance(gid, int) or gid <= 0:
            raise ValidationError("闸门ID必须为正整数")


def validate_discharge(discharge: float) -> None:
    if not isinstance(discharge, (int, float)) or isinstance(discharge, bool):
        raise ValidationError("下泄量必须是数字")
    if discharge <= 0:
        raise ValidationError("下泄量必须大于0")


def gate_effective_capacity(gate: Dict[str, Any]) -> float:
    """闸门有效过流能力：可用状态下为额定能力，其余为0。"""
    if gate.get("status") != GATE_AVAILABLE:
        return 0.0
    return float(gate.get("capacity", 0.0))


def remaining_capacity(gate: Dict[str, Any], reserved_discharge: float) -> float:
    """剩余过流能力 = 有效能力 - 已预占下泄量。"""
    effective = gate_effective_capacity(gate)
    return effective - reserved_discharge


def capacity_gap(requested: float, remaining: float) -> float:
    """容量缺口 = max(0, 请求量 - 剩余量)。"""
    return max(0.0, requested - remaining)


def allocate_status(gap: float) -> str:
    """根据缺口决定预占状态：无缺口→已占足，有缺口→排队。"""
    if gap <= 0:
        return RES_RESERVED
    return RES_QUEUED


def is_fully_reserved(capacity_status: Optional[str]) -> bool:
    """指令是否已占足容量。"""
    return capacity_status == CAP_RESERVED


def can_authorize(capacity_status: Optional[str]) -> bool:
    """总工只能授权已占足容量的指令。"""
    return is_fully_reserved(capacity_status)


def authorization_blocker(capacity_status: Optional[str]) -> Optional[str]:
    """授权拦截原因。"""
    if capacity_status == CAP_QUEUED:
        return "指令容量未占足，仍在排队，不能授权"
    if capacity_status == CAP_PENDING_REVIEW:
        return "历史指令缺少容量依据，已升级为待补核，不能授权"
    if capacity_status == CAP_EXECUTED:
        return "指令已执行，不能重复授权"
    return None


def is_historical(item: Dict[str, Any]) -> bool:
    """历史指令：提交时未绑定闸门组合/时段/下泄量，缺少容量依据。"""
    gate_ids = item.get("gate_ids")
    return not gate_ids


def needs_recalculation(item: Dict[str, Any]) -> bool:
    """未执行指令需要参与容量重算。"""
    return item.get("status") in UNEXECUTED_STATES


def keeps_snapshot(item: Dict[str, Any]) -> bool:
    """已执行指令保留当时快照，不参与重算。"""
    return item.get("status") in EXECUTED_STATES


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """两个时段是否重叠（半开区间，首尾相接不算重叠）。"""
    return a_start < b_end and b_start < a_end
