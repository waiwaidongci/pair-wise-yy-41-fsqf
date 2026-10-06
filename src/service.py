from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (APPROVAL_LEVEL_ROLES, AUDIT_ROLES, CREATE_ROLES, DETOUR_KINDS,
                    EMERGENCY_ROLES, ENTITY, NOTICE_ENTITY, RECORD_ROLES, RESTORE_ROLES,
                    TITLE, VIEW_ROLES, adjacent_bridge_restrictions,
                    approval_role_for_level, can_transition_notice, completion_blockers,
                    downstream_unloaded, escalation_required, is_published, is_unpublished,
                    network_capacity_check, next_approval_level, priority_score,
                    response_deadline_hours, role_for_transition, validate_notice_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ------------------------------------------------------------------
    # 桥梁告警（items）
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
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
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
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        # 气象、交通通告或巡检记录变化后，未发布的限行通告失效重算
        self._invalidate_unpublished(item_id, actor)
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
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

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

    # ------------------------------------------------------------------
    # 限行通告（restriction notices）
    # ------------------------------------------------------------------

    def create_restriction(self, bridge_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        """值班员发起限行通告。"""
        ensure_role(role, set(['duty_officer']))
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(bridge_id)
        severity = normalize_severity(payload.get("severity", item["severity"]))
        notice = self.repository.create_notice(bridge_id, severity, actor)
        self.repository.append_audit("notice_create", NOTICE_ENTITY, notice["id"], actor, {
            "bridge_id": bridge_id, "severity": severity,
        })
        return self._enrich_notice(notice)

    def approve_notice(self, notice_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """桥梁工程师和安全监督员两级放行。"""
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        level = next_approval_level(notice["status"])
        if level is None:
            raise ConflictError("当前状态无需审批")
        ensure_role(role, approval_role_for_level(level))
        comment = payload.get("comment")
        evidence = payload.get("evidence")
        if comment is not None:
            comment = require_text(comment, "comment", 500)
        if evidence is not None:
            evidence = require_text(evidence, "evidence", 500)
        # 记录审批
        self.repository.add_approval(notice_id, level, actor, 'approved',
                                     comment, evidence)
        # 推进状态
        target = 'engineer_approved' if level == 'engineer' else 'supervisor_approved'
        validate_notice_transition(notice["status"], target)
        updated = self.repository.update_notice_status(
            notice_id, target, notice["version"], actor)
        self.repository.append_audit("notice_approve", NOTICE_ENTITY, notice_id, actor, {
            "level": level, "from": notice["status"], "to": target,
        })
        return self._enrich_notice(updated)

    def publish_notice(self, notice_id: int, payload: Dict[str, Any],
                       actor: str, role: str,
                       _fail_after: Optional[str] = None) -> Dict[str, Any]:
        """发布限行通告。

        流程：网络容量核算 → 占用发布额度（幂等）→ 保存快照 → 状态置为已发布。
        写入失败后从剩余步骤恢复，已占用额度只记一次。
        """
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        if notice["status"] != 'supervisor_approved':
            raise ConflictError("通告需经两级审批后方可发布")
        ensure_role(role, set(['safety_supervisor', 'bridge_engineer']))

        # 步骤1：网络容量核算
        closure_load = require_number(payload.get("closure_load", 0), "closure_load")
        detour_routes = self.repository.list_detour_routes(notice["bridge_id"])
        ok, capacity_details = network_capacity_check(closure_load, detour_routes)
        adjacent_restrictions = adjacent_bridge_restrictions(closure_load, detour_routes)
        if not ok:
            raise ConflictError(
                f"路网容量不足，无法发布限行通告：{capacity_details}")
        if adjacent_restrictions:
            names = "、".join(r["bridge"] for r in adjacent_restrictions)
            raise ConflictError(
                f"相邻桥承接绕行流量超限，必须先限行：{names}")
        if _fail_after == 'capacity':
            raise RuntimeError("模拟容量核算后写入失败")

        # 步骤2：占用发布额度（幂等，只记一次）
        quota = self.repository.occupy_quota(notice["bridge_id"], notice_id, actor)
        if _fail_after == 'quota':
            raise RuntimeError("模拟额度占用后写入失败")

        # 步骤3：保存快照（已公布内容保留原始快照）
        snapshot = self._build_snapshot(notice["bridge_id"], closure_load,
                                        capacity_details, detour_routes)
        if _fail_after == 'snapshot':
            raise RuntimeError("模拟快照保存后写入失败")

        # 步骤4：状态置为已发布
        now = self._now()
        updated = self.repository.update_notice_status(
            notice_id, 'published', notice["version"], actor,
            snapshot=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            published_at=now)
        # 记录绕行影响（供下游卸载判断）
        for route in detour_routes:
            if route['kind'] == 'adjacent_bridge':
                extra = closure_load  # 相邻桥承接的绕行流量
                if extra > 0:
                    self.repository.add_detour_impact(
                        notice["bridge_id"], notice_id, route['name'], extra)
        self.repository.append_audit("notice_publish", NOTICE_ENTITY, notice_id, actor, {
            "bridge_id": notice["bridge_id"], "closure_load": closure_load,
            "quota_id": quota["id"], "snapshot": snapshot,
        })
        return self._enrich_notice(updated)

    def emergency_publish(self, notice_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        """安全监督员紧急放行：必须绑定现场证据和复核人。"""
        ensure_role(role, EMERGENCY_ROLES)
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        if not is_unpublished(notice["status"]):
            raise ConflictError("当前状态不允许紧急放行")
        evidence = require_text(payload.get("evidence"), "evidence", 500)
        reviewer = require_text(payload.get("reviewer"), "reviewer", 100)
        if reviewer == actor:
            raise ValidationError("复核人不能与放行人为同一人")
        # 占用额度
        quota = self.repository.occupy_quota(notice["bridge_id"], notice_id, actor)
        # 保存快照
        snapshot = self._build_snapshot(notice["bridge_id"], 0, {}, [])
        now = self._now()
        updated = self.repository.update_notice_status(
            notice_id, 'published', notice["version"], actor,
            snapshot=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            published_at=now, reviewer=reviewer, evidence=evidence, emergency=1)
        self.repository.append_audit("notice_emergency_publish", NOTICE_ENTITY,
                                     notice_id, actor, {
                                         "bridge_id": notice["bridge_id"],
                                         "reviewer": reviewer, "evidence": evidence,
                                         "quota_id": quota["id"],
                                     })
        return self._enrich_notice(updated)

    def review_emergency(self, notice_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """复核紧急放行：复核不通过就撤销发布并释放额度。"""
        ensure_role(role, set(['reviewer', 'safety_supervisor']))
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        if not notice["emergency"] or not is_published(notice["status"]):
            raise ConflictError("该通告不是紧急放行状态，无需复核")
        if notice["reviewer"] and notice["reviewer"] != actor:
            raise PermissionDenied("复核人与登记的复核人不一致")
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise ValidationError("approved必须是布尔值")
        if approved:
            self.repository.append_audit("notice_review", NOTICE_ENTITY,
                                         notice_id, actor, {"result": "approved"})
            return self._enrich_notice(notice)
        # 复核不通过：撤销发布并释放额度
        validate_notice_transition(notice["status"], 'revoked')
        updated = self.repository.update_notice_status(
            notice_id, 'revoked', notice["version"], actor)
        self.repository.release_quota(notice_id)
        self.repository.append_audit("notice_review", NOTICE_ENTITY,
                                     notice_id, actor, {"result": "revoked"})
        return self._enrich_notice(updated)

    def restore_notice(self, notice_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """恢复限行：原桥恢复申请要等下游卸载完成。"""
        ensure_role(role, RESTORE_ROLES)
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        if not is_published(notice["status"]):
            raise ConflictError("只有已发布的通告才能恢复")
        # 下游卸载检查
        impacts = self.repository.list_detour_impacts(notice["bridge_id"])
        active = [i for i in impacts if i["notice_id"] == notice_id and i["extra_load"] > 0]
        unloaded, blocked_by = downstream_unloaded(notice["bridge_id"], active)
        if not unloaded:
            raise ConflictError(
                f"下游桥{blocked_by}尚未卸载完成，暂不能恢复原桥通行")
        validate_notice_transition(notice["status"], 'restored')
        updated = self.repository.update_notice_status(
            notice_id, 'restored', notice["version"], actor)
        self.repository.release_quota(notice_id)
        self.repository.append_audit("notice_restore", NOTICE_ENTITY,
                                     notice_id, actor, {"bridge_id": notice["bridge_id"]})
        return self._enrich_notice(updated)

    def recover_notice(self, notice_id: int, actor: str, role: str) -> Dict[str, Any]:
        """从剩余步骤恢复发布流程。

        写入失败后，根据通告当前状态判断从哪一步继续。
        已占用额度只记一次（occupy_quota 幂等）。
        """
        ensure_role(role, set(['safety_supervisor', 'bridge_engineer']))
        actor = require_text(actor, "actor", 100)
        notice = self.repository.get_notice(notice_id)
        if notice["status"] == 'published':
            return self._enrich_notice(notice)
        if notice["status"] != 'supervisor_approved':
            raise ConflictError("当前状态无法恢复发布")
        # 检查额度是否已占用（幂等）
        holder = self.repository.get_quota_holder(notice["bridge_id"])
        if holder and holder["notice_id"] == notice_id:
            # 额度已占用，直接继续发布（快照按当前条件重建）
            detour_routes = self.repository.list_detour_routes(notice["bridge_id"])
            snapshot = self._build_snapshot(notice["bridge_id"], 0, {}, detour_routes)
            now = self._now()
            updated = self.repository.update_notice_status(
                notice_id, 'published', notice["version"], actor,
                snapshot=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                published_at=now)
            self.repository.append_audit("notice_recover", NOTICE_ENTITY,
                                         notice_id, actor, {"from": "quota_occupied"})
            return self._enrich_notice(updated)
        # 额度未占用，重新走发布流程（额度只记一次）
        return self.publish_notice(notice_id, {}, actor, role)

    def list_notices(self, bridge_id: int, role: str,
                     status: Optional[str] = None) -> list:
        self._view(role)
        return [self._enrich_notice(n)
                for n in self.repository.list_notices(bridge_id, status)]

    def get_notice(self, notice_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_notice(self.repository.get_notice(notice_id))

    def quota_status(self, bridge_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        holder = self.repository.get_quota_holder(bridge_id)
        return {
            "bridge_id": bridge_id,
            "occupied": holder is not None,
            "holder": holder,
        }

    def add_detour_route(self, bridge_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, set(['traffic_authority', 'bridge_engineer']))
        actor = require_text(actor, "actor", 100)
        kind = payload.get("kind")
        if kind not in DETOUR_KINDS:
            raise ValidationError(f"kind必须是{DETOUR_KINDS}之一")
        name = require_text(payload.get("name"), "name", 200)
        capacity = require_number(payload.get("capacity"), "capacity")
        current_load = require_number(payload.get("current_load", 0), "current_load")
        route = self.repository.add_detour_route(bridge_id, kind, name, capacity,
                                                 current_load, actor)
        self.repository.append_audit("detour_route_add", ENTITY, bridge_id, actor, {
            "route_id": route["id"], "kind": kind, "name": name,
        })
        return route

    def list_detour_routes(self, bridge_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_detour_routes(bridge_id)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _invalidate_unpublished(self, bridge_id: int, actor: str) -> None:
        """气象、交通通告或巡检记录变化后，未发布的限行通告失效重算。"""
        notices = self.repository.list_notices(bridge_id)
        for notice in notices:
            if is_unpublished(notice["status"]):
                validate_notice_transition(notice["status"], 'invalidated')
                self.repository.update_notice_status(
                    notice["id"], 'invalidated', notice["version"], actor)
                self.repository.append_audit("notice_invalidate", NOTICE_ENTITY,
                                             notice["id"], actor, {
                                                 "reason": "条件变化，未发布通告失效",
                                             })

    def _build_snapshot(self, bridge_id: int, closure_load: float,
                        capacity_details: Dict[str, Any],
                        detour_routes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构建发布快照（已公布内容保留原始快照）。"""
        item = self.repository.get_item(bridge_id)
        records = self.repository.list_records(bridge_id)
        return {
            "bridge_id": bridge_id,
            "severity": item["severity"],
            "quantity": item["quantity"],
            "threshold": item["threshold"],
            "closure_load": closure_load,
            "capacity": capacity_details,
            "detour_routes": [
                {"kind": r["kind"], "name": r["name"],
                 "capacity": r["capacity"], "current_load": r["current_load"]}
                for r in detour_routes
            ],
            "records": [
                {"kind": r["kind"], "status": r["status"]} for r in records
            ],
            "snapshot_at": self._now(),
        }

    @staticmethod
    def _now() -> str:
        from .audit import utc_now
        return utc_now()

    def _enrich_notice(self, notice: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(notice)
        if result.get("snapshot"):
            try:
                result["snapshot"] = json.loads(result["snapshot"])
            except (TypeError, json.JSONDecodeError):
                pass
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
