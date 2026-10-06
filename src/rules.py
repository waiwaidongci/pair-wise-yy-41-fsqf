from __future__ import annotations

from .domain import ConflictError, ValidationError

TITLE = '桥梁结构监测与限行决策'
ENTITY = '桥梁告警'
NOTICE_ENTITY = '限行通告'
ID_PREFIX = 'BM'

SEVERITIES = ['normal', 'watch', 'warning', 'critical']
STATES = ['normal', 'warning', 'restricted', 'closed', 'restored']
TRANSITIONS = {
    'normal': ['warning'],
    'warning': ['restricted'],
    'restricted': ['closed'],
    'closed': ['restored'],
    'restored': [],
}
TRANSITION_ROLES = {
    'warning': ['sensor_operator'],
    'restricted': ['bridge_engineer'],
    'closed': ['traffic_authority'],
    'restored': ['bridge_engineer'],
}
CREATE_ROLES = set(['sensor_operator'])
RECORD_ROLES = set(['sensor_operator', 'bridge_engineer'])
AUDIT_ROLES = set(['bridge_engineer', 'viewer'])
VIEW_ROLES = set(['sensor_operator', 'bridge_engineer', 'traffic_authority', 'viewer'])

# 限行通告状态机：值班员发起 → 桥梁工程师审批 → 安全监督员审批 → 发布
NOTICE_STATES = ['pending_approval', 'engineer_approved', 'supervisor_approved',
                 'published', 'invalidated', 'restored', 'revoked']
NOTICE_TRANSITIONS = {
    'pending_approval': ['engineer_approved', 'invalidated'],
    'engineer_approved': ['supervisor_approved', 'invalidated'],
    'supervisor_approved': ['published', 'invalidated'],
    'published': ['restored', 'revoked'],
    'invalidated': [],
    'restored': [],
    'revoked': [],
}
# 审批级别 → 允许的角色
APPROVAL_LEVEL_ROLES = {
    'engineer': ['bridge_engineer'],
    'supervisor': ['safety_supervisor'],
}
# 紧急放行允许的角色
EMERGENCY_ROLES = set(['safety_supervisor'])
# 恢复申请允许的角色
RESTORE_ROLES = set(['bridge_engineer', 'safety_supervisor'])
# 发布额度：每座桥梁同时只能有一条生效通告
QUOTA_LIMIT_PER_BRIDGE = 1

SEVERITY_WEIGHT = {'normal': 1.0, 'watch': 3.0, 'warning': 6.0, 'critical': 9.0}
DEADLINE_HOURS = {'normal': 72, 'watch': 24, 'warning': 8, 'critical': 4}
TERMINAL_STATES = set(['restored'])

# 网络容量核算：公交绕行、救护通道、相邻桥梁限行一起算
DETOUR_KINDS = ['bus', 'ambulance', 'adjacent_bridge', 'other']


def priority_score(severity, quantity=0.0, threshold=1.0, open_records=0):
    if severity not in SEVERITY_WEIGHT:
        raise ValidationError("unknown severity")
    ratio = quantity / threshold if threshold > 0 else 1.0
    return max(0, min(10, int(round(
        SEVERITY_WEIGHT[severity]
        + min(4.0, ratio * 4.0)
        + min(3.0, float(open_records))))))


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
# 限行通告状态机
# ---------------------------------------------------------------------------

def can_transition_notice(current, target):
    return target in NOTICE_TRANSITIONS.get(current, [])


def validate_notice_transition(current, target):
    if current not in NOTICE_STATES or target not in NOTICE_STATES:
        raise ValidationError("未知通告状态")
    if not can_transition_notice(current, target):
        raise ConflictError(f"通告不能从{current}转换到{target}")


def approval_role_for_level(level):
    if level not in APPROVAL_LEVEL_ROLES:
        raise ValidationError("未知审批级别")
    return set(APPROVAL_LEVEL_ROLES[level])


def next_approval_level(current_status):
    """返回当前状态下一个待审批级别，若无需审批则返回 None。"""
    if current_status == 'pending_approval':
        return 'engineer'
    if current_status == 'engineer_approved':
        return 'supervisor'
    return None


def is_unpublished(status):
    return status in ('pending_approval', 'engineer_approved', 'supervisor_approved')


def is_published(status):
    return status == 'published'


# ---------------------------------------------------------------------------
# 网络容量核算
# ---------------------------------------------------------------------------

def network_capacity_check(closure_load, detour_routes):
    """封路占路网容量，公交绕行、救护通道和相邻桥梁限行一起核算。

    detour_routes: list of dict(kind, capacity, current_load)
    返回 (ok, details)。ok 为 True 表示路网可承载绕行流量。
    """
    if closure_load < 0:
        raise ValidationError("closure_load不能为负")
    total_available = 0.0
    bottlenecks = []
    for route in detour_routes:
        capacity = float(route.get('capacity', 0))
        current = float(route.get('current_load', 0))
        available = max(0.0, capacity - current)
        total_available += available
        if available <= 0:
            bottlenecks.append(route.get('name', route.get('kind', 'unknown')))
    ok = closure_load <= total_available and not bottlenecks
    details = {
        'closure_load': closure_load,
        'total_available': total_available,
        'bottlenecks': bottlenecks,
        'detour_count': len(detour_routes),
    }
    return ok, details


def adjacent_bridge_restrictions(closure_load, detour_routes):
    """相邻桥承接绕行流量时，返回需要限行的相邻桥清单。

    当绕行流量超过相邻桥剩余容量时，相邻桥必须限行以削减自身荷载。
    """
    restrictions = []
    for route in detour_routes:
        if route.get('kind') != 'adjacent_bridge':
            continue
        capacity = float(route.get('capacity', 0))
        current = float(route.get('current_load', 0))
        available = max(0.0, capacity - current)
        if closure_load > available:
            overflow = closure_load - available
            restrictions.append({
                'bridge': route.get('name'),
                'overflow': overflow,
                'reason': '相邻桥承接绕行流量超限，必须限行削减荷载',
            })
    return restrictions


def downstream_unloaded(bridge_id, active_detours):
    """原桥恢复申请要等下游卸载完成。

    active_detours: 该桥封路后将流量导向相邻桥的记录列表。
    只有当所有相邻桥都已卸载（无额外绕行荷载）时才允许恢复。
    """
    for detour in active_detours:
        extra_load = float(detour.get('extra_load', 0))
        if extra_load > 0:
            return False, detour.get('adjacent_bridge')
    return True, None
