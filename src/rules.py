from __future__ import annotations

from .domain import (CONTEXT_KINDS, LEVELS, PENDING_STATES, STATES,
                     ValidationError, require_choice)

TITLE = '桥梁限行发布审批'
ENTITY = '限行通告'
BRIDGE_ENTITY = '桥梁'

# 状态机：
# 值班员报预警 pending_engineer -> 桥梁工程师一级放行 pending_supervisor
#   -> 安全监督员二级放行 restricted
# 安全监督员紧急放行 pending_* -> emergency_pending_review
#   -> 复核通过 restricted / 复核驳回 revoked
# 输入变化：未发布的 pending_* -> invalidated（已发布的保留快照，不重算）
# 恢复：restricted -> restored（须下游卸载完成）
TRANSITIONS = {
    'pending_engineer': ['pending_supervisor', 'emergency_pending_review', 'invalidated'],
    'pending_supervisor': ['restricted', 'emergency_pending_review', 'invalidated'],
    'emergency_pending_review': ['restricted', 'revoked'],
    'restricted': ['restored'],
    'invalidated': [],
    'revoked': [],
    'restored': [],
}

# 每个迁移允许执行的角色
TRANSITION_ROLES = {
    ('pending_engineer', 'pending_supervisor'): ['bridge_engineer'],
    ('pending_engineer', 'emergency_pending_review'): ['safety_supervisor'],
    ('pending_engineer', 'invalidated'): ['duty_officer', 'bridge_engineer',
                                          'safety_supervisor', 'traffic_ops'],
    ('pending_supervisor', 'restricted'): ['safety_supervisor'],
    ('pending_supervisor', 'emergency_pending_review'): ['safety_supervisor'],
    ('pending_supervisor', 'invalidated'): ['duty_officer', 'bridge_engineer',
                                            'safety_supervisor', 'traffic_ops'],
    ('emergency_pending_review', 'restricted'): ['safety_supervisor'],
    ('emergency_pending_review', 'revoked'): ['safety_supervisor'],
    ('restricted', 'restored'): ['bridge_engineer'],
}

CREATE_ROLES = {'duty_officer'}
BRIDGE_CREATE_ROLES = {'bridge_engineer', 'traffic_ops'}
CONTEXT_ROLES = {'duty_officer', 'bridge_engineer', 'traffic_ops'}
OFFLOAD_ROLES = {'traffic_ops', 'bridge_engineer'}
VIEW_ROLES = {'duty_officer', 'bridge_engineer', 'safety_supervisor',
              'traffic_ops', 'viewer'}
AUDIT_ROLES = {'bridge_engineer', 'safety_supervisor', 'traffic_ops', 'viewer'}

# 不同限行级别分流到相邻桥/路网的比例，以及其中公交占比、救护通道预留
DIVERSION_RATIO = {'load_limit': 0.20, 'lane_close': 0.50, 'full_close': 1.0}
BUS_SHARE = {'load_limit': 0.10, 'lane_close': 0.15, 'full_close': 0.20}
# 相邻桥已有限行时，其剩余容量折算系数
NEIGHBOR_CAPACITY_FACTOR = {'normal': 1.0, 'load_limit': 0.75,
                            'lane_close': 0.45, 'full_close': 0.0}
# 全桥封闭额外占用路网容量预算
NETWORK_COST = {'load_limit': 10.0, 'lane_close': 30.0, 'full_close': 60.0}

# 紧急放行：复核人不能等于紧急放行人
EMERGENCY_REVIEW_STATES = {'emergency_pending_review'}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, [])


def validate_transition(current: str, target: str) -> None:
    if current not in STATES or target not in STATES:
        raise ValidationError("未知状态")
    if not can_transition(current, target):
        from .domain import ConflictError
        raise ConflictError(f"不能从{current}转换到{target}")


def roles_for_transition(current: str, target: str):
    return set(TRANSITION_ROLES.get((current, target), []))


def diversion_vehicles(level: str, daily_vehicles: float) -> float:
    """限行后必须分流到相邻桥/路网的流量。"""
    require_choice(level, "level", LEVELS)
    return daily_vehicles * DIVERSION_RATIO[level]


def diversion_buses(level: str, daily_buses: float) -> float:
    require_choice(level, "level", LEVELS)
    return daily_buses * DIVERSION_RATIO[level] * (1.0 + BUS_SHARE[level])


def ambulance_reserve_required(level: str, ambulance_required: float) -> float:
    # 全桥封闭必须保障救护通道；低级别限行按申报量预留
    require_choice(level, "level", LEVELS)
    if level == 'full_close':
        return max(ambulance_required, 1.0)
    return ambulance_required


def neighbor_capacity(neighbor) -> float:
    """相邻桥可承接绕行的剩余容量；相邻桥自身限行/封闭时容量折减。"""
    status = neighbor.get('status', 'normal')
    factor = NEIGHBOR_CAPACITY_FACTOR.get(status, 1.0)
    return max(0.0, float(neighbor['capacity']) * factor)


def assess_capacity(bridge: dict, level: str, neighbor,
                    network_budget: float, ambulance_required: float = 0.0) -> dict:
    """
    封路占路网容量，公交绕行、救护通道和相邻桥梁限行一起核算。
    返回测算结果；blockers 非空表示当前不能放行/封闭。
    bridge 需含 capacity / daily_vehicles / daily_buses；
    neighbor 为 None 表示无相邻桥（绕行全部走路网），需含 capacity/status。
    """
    require_choice(level, "level", LEVELS)
    diverted = diversion_vehicles(level, float(bridge['daily_vehicles']))
    buses = diversion_buses(level, float(bridge['daily_buses']))
    ambulance = ambulance_reserve_required(level, float(ambulance_required))
    network_cost = NETWORK_COST[level]

    blockers = []
    neighbor_free = neighbor_capacity(neighbor) if neighbor else 0.0
    to_network = diverted
    if neighbor is not None:
        to_network = max(0.0, diverted - neighbor_free)
        if diverted > neighbor_free:
            blockers.append(
                f"相邻桥剩余容量{neighbor_free:.1f}不足以承接绕行流量{diverted:.1f}")
    if level == 'full_close' and neighbor is not None and \
            neighbor.get('status') == 'full_close':
        blockers.append("相邻桥已全桥封闭，绕行路径中断")
    if buses > network_budget:
        blockers.append(f"公交绕行需求{buses:.1f}超过路网公交预算{network_budget:.1f}")
    if ambulance > network_budget:
        blockers.append(f"救护通道预留{ambulance:.1f}超过路网容量预算{network_budget:.1f}")
    if to_network + network_cost > network_budget:
        blockers.append(
            f"路网占用{network_cost:.1f}+分流{to_network:.1f}超过容量预算{network_budget:.1f}")

    return {
        "level": level,
        "diverted_vehicles": round(diverted, 2),
        "diverted_buses": round(buses, 2),
        "ambulance_reserve": round(ambulance, 2),
        "network_cost": network_cost,
        "neighbor_free_capacity": round(neighbor_free, 2),
        "to_network": round(to_network, 2),
        "network_budget": network_budget,
        "feasible": not blockers,
        "blockers": blockers,
    }


def restoration_blocked(notice: dict, remaining_diversion: float) -> bool:
    """相邻桥承接绕行流量时，原桥恢复申请要等下游卸载完成。"""
    if notice.get('neighbor_id') is None:
        return False
    return remaining_diversion > 0
