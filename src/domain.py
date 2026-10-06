from __future__ import annotations
from typing import Any, Dict, Optional


class ErrorKind:
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"


class DomainError(Exception):
    kind = ErrorKind.VALIDATION

    def __init__(self, message: str):
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


class QuotaConflict(ConflictError):
    """同桥发布额度已被先到的审批占用。"""

    def __init__(self, message: str, holder: Optional[str] = None,
                 notice_id: Optional[int] = None):
        super().__init__(message)
        self.holder = holder
        self.notice_id = notice_id

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.__class__.__name__, "message": self.message,
                "holder": self.holder, "notice_id": self.notice_id}


# 值班员预警；桥梁工程师一级放行；安全监督员二级放行/紧急放行；交通调度负责通告与卸载；viewer只读
ROLES = ['duty_officer', 'bridge_engineer', 'safety_supervisor',
         'traffic_ops', 'viewer']

# 限行级别：限载 / 车道封闭 / 全桥封闭
LEVELS = ['load_limit', 'lane_close', 'full_close']
LEVEL_LABELS = {'load_limit': '限载', 'lane_close': '车道封闭', 'full_close': '全桥封闭'}

# 通告状态：待工程师放行 -> 待监督员放行 -> 已发布
# 紧急放行 -> 紧急待复核 ->（通过）已发布 /（驳回）已撤销
# 未发布通告在输入变化后失效；已发布通告最终恢复
STATES = ['pending_engineer', 'pending_supervisor', 'restricted',
          'emergency_pending_review', 'invalidated', 'revoked', 'restored']

# 尚未发布、额度仍被占用的状态
PENDING_STATES = {'pending_engineer', 'pending_supervisor'}
# 已经对公众发布、需保留原始快照的状态
PUBLISHED_STATES = {'restricted', 'emergency_pending_review'}
TERMINAL_STATES = {'invalidated', 'revoked', 'restored'}

# 触发未发布通告失效重算的三类记录
CONTEXT_KINDS = ['weather', 'traffic_notice', 'inspection']
CONTEXT_KIND_LABELS = {'weather': '气象', 'traffic_notice': '交通通告',
                       'inspection': '巡检记录'}


def require_text(value: Any, field: str, max_length: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def require_choice(value: Any, field: str, choices) -> str:
    if value not in choices:
        raise ValidationError(f"{field}不在允许范围内: {sorted(choices)}")
    return value


def require_number(value: Any, field: str, minimum: float = 0.0,
                   maximum: Optional[float] = None) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if number < minimum:
        raise ValidationError(f"{field}不能小于{minimum}")
    if maximum is not None and number > maximum:
        raise ValidationError(f"{field}不能大于{maximum}")
    return number


def ensure_role(role: str, allowed) -> None:
    if role not in allowed:
        raise PermissionDenied("当前角色无权执行该操作")
