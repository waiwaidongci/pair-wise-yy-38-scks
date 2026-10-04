from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional


class ErrorKind:
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    CAPACITY = "capacity"


class DomainError(Exception):
    kind = ErrorKind.VALIDATION

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    kind = ErrorKind.VALIDATION


class NotFoundError(DomainError):
    kind = ErrorKind.NOT_FOUND


class PermissionDenied(DomainError):
    kind = ErrorKind.FORBIDDEN


class ConflictError(DomainError):
    kind = ErrorKind.CONFLICT


class CapacityError(DomainError):
    """容量不足或容量依据不满足时抛出。"""
    kind = ErrorKind.CAPACITY


SEVERITIES = ['routine', 'attention', 'urgent', 'emergency']
STATES = ['draft', 'checked', 'authorized', 'executed', 'closed']
ROLES = ['duty_officer', 'chief_engineer', 'dispatcher', 'viewer']

# 闸门可用状态
GATE_STATUSES = ['available', 'maintenance', 'closed']
# 指令容量状态
CAPACITY_STATUSES = ['reserved', 'queued', 'pending_review', 'executed']
# 预占记录状态
RESERVATION_STATUSES = ['reserved', 'queued', 'executed', 'released']
# 容量批次状态
BATCH_STATUSES = ['active', 'superseded']


@dataclass(frozen=True)
class Item:
    id: int
    title: str
    description: str
    severity: str
    quantity: float
    threshold: float
    status: str
    version: int
    external_ref: Optional[str]
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Record:
    id: int
    item_id: int
    kind: str
    detail: str
    status: str
    external_ref: Optional[str]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class AuditEntry:
    id: int
    action: str
    entity_type: str
    entity_id: int
    actor: str
    detail: Dict[str, Any]
    previous_hash: str
    entry_hash: str
    created_at: str


@dataclass(frozen=True)
class Gate:
    id: int
    code: str
    name: str
    capacity: float
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WaterObservation:
    id: int
    observed_at: str
    reservoir_level: float
    inflow: float
    downstream_alert: float
    created_by: str
    created_at: str


@dataclass(frozen=True)
class CapacityBatch:
    id: int
    batch_no: str
    item_id: int
    basis_observation_id: Optional[int]
    status: str
    evaluated_at: str
    created_by: str


@dataclass(frozen=True)
class CapacityReservation:
    id: int
    batch_id: int
    item_id: int
    gate_id: int
    period_start: str
    period_end: str
    requested_discharge: float
    allocated_discharge: float
    gap: float
    status: str
    snapshot: Optional[Dict[str, Any]]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IdempotencyKey:
    id: int
    request_no: str
    actor: str
    request_type: str
    request_hash: str
    response: Dict[str, Any]
    created_at: str


def require_text(value, field, max_length=2000):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def normalize_severity(value):
    if value not in SEVERITIES:
        raise ValidationError("severity不在允许范围内")
    return value


def require_number(value, field, minimum=0.0):
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if number < minimum:
        raise ValidationError(f"{field}不能小于{minimum}")
    return number


def ensure_role(role, allowed):
    if role not in allowed:
        raise PermissionDenied("当前角色无权执行该操作")
