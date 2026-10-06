from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (CONTEXT_KINDS, LEVELS, LEVEL_LABELS, PENDING_STATES, STATES,
                     ConflictError, PermissionDenied, QuotaConflict, ensure_role,
                     require_choice, require_number, require_text)
from .repository import BRIDGE_STATUSES, Repository
from .rules import (AUDIT_ROLES, BRIDGE_CREATE_ROLES, BRIDGE_ENTITY,
                    BUS_SHARE, CONTEXT_ROLES, CREATE_ROLES, DIVERSION_RATIO,
                    ENTITY, NETWORK_COST, OFFLOAD_ROLES, TITLE, VIEW_ROLES,
                    assess_capacity, diversion_buses, diversion_vehicles,
                    roles_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    # ---------- 桥梁 ----------
    def register_bridge(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, BRIDGE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        capacity = require_number(payload.get("capacity"), "capacity", 0.01)
        daily_vehicles = require_number(payload.get("daily_vehicles", 0),
                                        "daily_vehicles")
        daily_buses = require_number(payload.get("daily_buses", 0), "daily_buses")
        neighbor_id = payload.get("neighbor_id")
        if neighbor_id is not None:
            neighbor_id = int(require_number(neighbor_id, "neighbor_id", 1))
            self.repository.get_bridge(neighbor_id)
        bridge = self.repository.create_bridge(name, capacity, daily_vehicles,
                                               daily_buses, neighbor_id, actor)
        self.repository.append_audit("register_bridge", BRIDGE_ENTITY, bridge["id"],
                                     actor, {"name": name, "capacity": capacity})
        return self.enrich_bridge(bridge)

    def list_bridges(self, role: str) -> list:
        ensure_role(role, VIEW_ROLES)
        return [self.enrich_bridge(b) for b in self.repository.list_bridges()]

    def get_bridge(self, bridge_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.enrich_bridge(self.repository.get_bridge(bridge_id))

    def enrich_bridge(self, bridge: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(bridge)
        quota = self.repository.get_quota(bridge["id"])
        result["quota"] = None if quota is None else {
            "holder_actor": quota["holder_actor"],
            "holder_notice_id": quota["holder_notice_id"],
            "operation_id": quota["operation_id"],
            "occupied_at": quota["occupied_at"],
        }
        return result

    # ---------- 内部：取相邻桥并做容量核算 ----------
    def _neighbor(self, bridge: Dict[str, Any]):
        if bridge.get("neighbor_id") is None:
            return None
        return self.repository.get_bridge(bridge["neighbor_id"])

    def _assess(self, bridge: Dict[str, Any], level: str, network_budget: float,
                ambulance_required: float) -> dict:
        return assess_capacity(bridge, level, self._neighbor(bridge),
                               network_budget, ambulance_required)

    # ---------- 值班员报预警（占用发布额度，失败可从剩余步骤恢复） ----------
    def submit_notice(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        bridge_id = int(require_number(payload.get("bridge_id"), "bridge_id", 1))
        level = require_choice(payload.get("level"), "level", LEVELS)
        reason = require_text(payload.get("reason"), "reason")
        network_budget = require_number(payload.get("network_budget", 100),
                                        "network_budget", 0.01)
        ambulance_required = require_number(payload.get("ambulance_required", 0),
                                            "ambulance_required")
        request_id = require_text(payload.get("request_id"), "request_id", 100)
        operation_id = f"submit:{request_id}"
        bridge = self.repository.get_bridge(bridge_id)
        quota = self.repository.get_quota(bridge_id)
        if quota is not None and quota["operation_id"] != operation_id:
            raise QuotaConflict(
                f"发布额度已被{quota['holder_actor']}的通告"
                f"#{quota['holder_notice_id']}占用",
                holder=quota["holder_actor"],
                notice_id=quota["holder_notice_id"])
        assessment = self._assess(bridge, level, network_budget, ambulance_required)

        # 完全恢复：上次调用已跑到审计步骤之后，直接返回结果
        if self.repository.is_step_done(operation_id, "audit_submit"):
            resumed_id = self.repository.operation_notice(operation_id)
            return self.enrich_notice(self.repository.get_notice(resumed_id))

        # 步骤1：占额度（同操作续跑复用，只记一次；他操作占用则冲突）
        self.repository.acquire_quota(operation_id, bridge_id, actor)
        # 步骤2：建未发布通告并回填额度
        notice = self.repository.insert_notice(
            operation_id, "insert_notice", bridge_id, actor, level, reason,
            request_id, network_budget, ambulance_required,
            bridge.get("neighbor_id"), assessment)
        # 步骤3：审计（步骤化，续跑不重复记账）
        self.repository.append_audit_step(
            operation_id, "audit_submit", "submit_notice", ENTITY, notice["id"],
            actor, {"bridge_id": bridge_id, "level": level,
                    "feasible": assessment["feasible"]})
        return self.enrich_notice(notice)

    # ---------- 两级放行 ----------
    def engineer_release(self, notice_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """一级放行：桥梁工程师。未发布，不施加绕行、不写快照。"""
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        target = "pending_supervisor"
        self._guard(notice, target, role)
        expected_version = self._expected_version(payload)
        updated = self.repository.publish_notice(
            f"engineer:{notice_id}:{expected_version}", "step", notice_id,
            expected_version, target, actor, bridge_status="normal",
            provisional=False, snapshot=None, diversion=None)
        self.repository.append_audit(
            "engineer_release", ENTITY, notice_id, actor,
            {"from": notice["status"], "to": target})
        return self.enrich_notice(updated)

    def supervisor_release(self, notice_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        """二级放行：安全监督员。发布前重新核算容量，已公布内容以快照固化。"""
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        expected_version = self._expected_version(payload)
        operation_id = f"release:{notice_id}:{expected_version}"
        # 恢复：发布已落库、仅审计未写完时跳过状态守卫
        if notice["status"] == "restricted":
            self.repository.append_audit_step(
                operation_id, "audit_release", "supervisor_release", ENTITY,
                notice_id, actor, {"resumed": True})
            return self.enrich_notice(self.repository.get_notice(notice_id))
        target = "restricted"
        self._guard(notice, target, role)
        bridge = self.repository.get_bridge(notice["bridge_id"])
        assessment = self._assess(bridge, notice["level"], notice["network_budget"],
                                  notice["ambulance_required"])
        if not assessment["feasible"]:
            raise ConflictError("容量核算不通过：" + "；".join(assessment["blockers"]))
        snapshot = self._snapshot(notice, bridge, assessment, actor, target)
        diversion = self._diversion_payload(notice, assessment)
        # 发布步骤（原子：状态+绕行+桥状态+快照）
        updated = self.repository.publish_notice(
            operation_id, "publish", notice_id, expected_version, target, actor,
            bridge_status=notice["level"], provisional=False, snapshot=snapshot,
            diversion=diversion)
        # 审计步骤（崩溃在两步之间时续跑，额度与发布均不重复）
        self.repository.append_audit_step(
            operation_id, "audit_release", "supervisor_release", ENTITY, notice_id,
            actor, {"from": notice["status"], "to": target,
                    "assessment": assessment})
        return self.enrich_notice(updated)

    # ---------- 紧急放行：必须绑定现场证据和复核人 ----------
    def emergency_release(self, notice_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, {'safety_supervisor'})
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        target = "emergency_pending_review"
        self._guard(notice, target, role)
        evidence = require_text(payload.get("evidence"), "evidence")
        reviewer = require_text(payload.get("reviewer"), "reviewer", 100)
        if reviewer == actor:
            from .domain import ValidationError
            raise ValidationError("复核人不能与紧急放行人相同")
        expected_version = self._expected_version(payload)
        bridge = self.repository.get_bridge(notice["bridge_id"])
        # 紧急放行跳过容量阻塞，但仍完成测算并存档
        assessment = self._assess(bridge, notice["level"], notice["network_budget"],
                                  notice["ambulance_required"])
        snapshot = self._snapshot(notice, bridge, assessment, actor, target,
                                  emergency={"evidence": evidence, "reviewer": reviewer})
        diversion = self._diversion_payload(notice, assessment)
        operation_id = f"emergency:{notice_id}:{expected_version}"
        updated = self.repository.publish_notice(
            operation_id, "publish", notice_id, expected_version, target, actor,
            bridge_status=notice["level"], provisional=True, snapshot=snapshot,
            diversion=diversion,
            emergency={"evidence": evidence, "reviewer": reviewer})
        self.repository.append_audit_step(
            operation_id, "audit_emergency", "emergency_release", ENTITY, notice_id,
            actor, {"reviewer": reviewer, "evidence": evidence,
                    "assessment_feasible": assessment["feasible"]})
        return self.enrich_notice(updated)

    def review_emergency(self, notice_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """复核：通过转正式发布；不通过撤销发布并释放额度。"""
        ensure_role(role, {'safety_supervisor'})
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        if notice["status"] != "emergency_pending_review":
            raise ConflictError("该通告不在紧急待复核状态")
        reviewer = actor
        if notice["released_by"] == reviewer:
            from .domain import ValidationError
            raise ValidationError("复核人不能与紧急放行人相同")
        if notice["emergency_reviewer"] not in (None, "", reviewer):
            raise ConflictError("该通告已绑定其他复核人")
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            from .domain import ValidationError
            raise ValidationError("approved必须是布尔值")
        detail = require_text(payload.get("detail"), "detail")
        expected_version = self._expected_version(payload)
        updated = self.repository.review_emergency(
            notice_id, expected_version, approved, reviewer, detail)
        self.repository.append_audit(
            "review_emergency", ENTITY, notice_id, reviewer,
            {"approved": approved, "detail": detail,
             "quota_released": not approved})
        return self.enrich_notice(updated)

    # ---------- 恢复：等下游卸载完成 ----------
    def restore(self, notice_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        self._guard(notice, "restored", role)
        expected_version = self._expected_version(payload)
        operation_id = f"restore:{notice_id}:{expected_version}"
        updated = self.repository.restore_notice(
            operation_id, "restore", notice_id, expected_version, actor)
        self.repository.append_audit_step(
            operation_id, "audit_restore", "restore", ENTITY, notice_id, actor,
            {"from": "restricted", "to": "restored"})
        return self.enrich_notice(updated)

    # ---------- 交通调度卸载 ----------
    def offload(self, notice_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        ensure_role(role, OFFLOAD_ROLES)
        actor = require_text(actor, "actor", 100)
        vehicles = require_number(payload.get("vehicles", 0), "vehicles")
        buses = require_number(payload.get("buses", 0), "buses")
        if vehicles == 0 and buses == 0:
            from .domain import ValidationError
            raise ValidationError("vehicles/buses至少一项大于0")
        load = self.repository.offload(notice_id, vehicles, buses)
        self.repository.append_audit(
            "offload", ENTITY, notice_id, actor,
            {"remaining_vehicles": load["remaining_vehicles"],
             "remaining_buses": load["remaining_buses"], "status": load["status"]})
        return load

    # ---------- 气象/交通通告/巡检记录变化：未发布通告失效重算 ----------
    def add_context(self, bridge_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, CONTEXT_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_choice(payload.get("kind"), "kind", CONTEXT_KINDS)
        detail = require_text(payload.get("detail"), "detail")
        ref = payload.get("ref")
        if ref is not None:
            ref = require_text(ref, "ref", 100)
        self.repository.get_bridge(bridge_id)
        reason = require_text(
            payload.get("reason", f"{kind}变化，未发布通告失效重算"), "reason")
        result = self.repository.add_context_and_invalidate(
            bridge_id, kind, detail, ref, actor, reason)
        for nid in result["invalidated_notice_ids"]:
            self.repository.append_audit(
                "invalidate_notice", ENTITY, nid, actor,
                {"context_kind": kind, "reason": reason, "quota_released": True})
        self.repository.append_audit(
            "add_context", BRIDGE_ENTITY, bridge_id, actor,
            {"kind": kind, "invalidated": result["invalidated_notice_ids"]})
        return result

    def list_context(self, bridge_id: int, role: str) -> list:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_context(bridge_id)

    # ---------- 查询 ----------
    def get_notice(self, notice_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.enrich_notice(self.repository.get_notice(notice_id))

    def list_notices(self, role: str, bridge_id: Optional[int] = None,
                     status: Optional[str] = None) -> list:
        ensure_role(role, VIEW_ROLES)
        return [self.enrich_notice(n)
                for n in self.repository.list_notices(bridge_id, status)]

    def get_diversion(self, notice_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        load = self.repository.get_diversion(notice_id)
        if load is None:
            from .domain import NotFoundError
            raise NotFoundError("该通告没有绕行承载记录")
        return load

    def audit(self, role: str, notice_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(notice_id)

    # ---------- 辅助 ----------
    @staticmethod
    def _expected_version(payload: Dict[str, Any]) -> int:
        value = payload.get("expected_version")
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        return value

    def _guard(self, notice: Dict[str, Any], target: str, role: str) -> None:
        validate_transition(notice["status"], target)
        ensure_role(role, roles_for_transition(notice["status"], target))

    @staticmethod
    def _diversion_payload(notice: Dict[str, Any], assessment: dict) -> dict:
        return {
            "neighbor_id": notice.get("neighbor_id"),
            "vehicles": assessment["diverted_vehicles"],
            "buses": assessment["diverted_buses"],
        }

    def _snapshot(self, notice: Dict[str, Any], bridge: Dict[str, Any],
                  assessment: dict, actor: str, target: str,
                  emergency: Optional[dict] = None) -> dict:
        """已发布内容保留原始快照；上下文为发布时点的数据。"""
        from .audit import utc_now
        context = self.repository.list_context(bridge["id"])
        return {
            "notice_id": notice["id"],
            "bridge_id": bridge["id"],
            "bridge_name": bridge["name"],
            "level": notice["level"],
            "level_label": LEVEL_LABELS[notice["level"]],
            "reason": notice["reason"],
            "published_as": target,
            "published_by": actor,
            "published_at": utc_now(),
            "assessment": assessment,
            "bridge_snapshot": {
                "status": bridge["status"],
                "capacity": bridge["capacity"],
                "daily_vehicles": bridge["daily_vehicles"],
                "daily_buses": bridge["daily_buses"],
            },
            "context_at_publish": [
                {"kind": c["kind"], "detail": c["detail"], "ref": c["ref"],
                 "created_at": c["created_at"]} for c in context],
            "emergency": emergency,
        }

    def enrich_notice(self, notice: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(notice)
        result["level_label"] = LEVEL_LABELS.get(result["level"], result["level"])
        result["is_pending"] = result["status"] in PENDING_STATES
        result["is_published"] = result["status"] in (
            'restricted', 'emergency_pending_review')
        load = self.repository.get_diversion(result["id"])
        result["diversion"] = None if load is None else {
            "neighbor_id": load["neighbor_id"],
            "remaining_vehicles": load["remaining_vehicles"],
            "remaining_buses": load["remaining_buses"],
            "status": load["status"],
        }
        quota = self.repository.get_quota(result["bridge_id"])
        result["quota_held"] = quota is not None and (
            quota["holder_notice_id"] == result["id"]
            or (quota["holder_notice_id"] is None
                and quota["operation_id"].endswith(result["request_id"])))
        return result
