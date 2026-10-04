from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class ErrorKind:
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"


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


# 闸门可用状态
GATE_STATES = ['available', 'unavailable']
# 指令生命周期：待排队 / 已预占 / 已授权 / 已执行 / 待补核
ORDER_STATES = ['queued', 'reserved', 'authorized', 'executed', 'recheck_pending']
ACTIVE_HOLD_STATES = ('reserved', 'authorized')
UNEXECUTED_STATES = ('queued', 'reserved', 'authorized', 'recheck_pending')
ROLES = ['duty_officer', 'chief_engineer', 'dispatcher', 'viewer']

# 容量比较容忍误差（m3/s），避免浮点尾差导致预占抖动
CAPACITY_EPSILON = 1e-6


@dataclass(frozen=True)
class Gate:
    id: int
    code: str
    name: str
    design_capacity: float
    status: str
    created_at: str


@dataclass(frozen=True)
class Order:
    id: int
    request_id: str
    purpose: str
    window_id: int
    discharge: float
    status: str
    basis_version: Optional[int]
    version: int
    created_by: str
    created_at: str
    updated_at: str
    executed_at: Optional[str]


@dataclass(frozen=True)
class OperationRecord:
    id: int
    order_id: int
    request_id: str
    detail: str
    snapshot: Dict[str, Any]
    actor: str
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


def require_text(value, field, max_length=2000):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def require_number(value, field, minimum=0.0, strict_minimum=False):
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if number < minimum or (strict_minimum and number <= minimum):
        comparator = "必须大于" if strict_minimum else "不能小于"
        raise ValidationError(f"{field}{comparator}{minimum}")
    return number


def require_timestamp(value, field):
    text = require_text(value, field, 40)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field}必须是ISO8601时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def ensure_role(role, allowed):
    if role not in allowed:
        raise PermissionDenied("当前角色无权执行该操作")
