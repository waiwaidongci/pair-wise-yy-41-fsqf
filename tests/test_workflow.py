import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, QuotaConflict
from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.svc = Service(self.repo)
        # B为相邻承接桥，容量充足
        self.b = self.svc.register_bridge(
            {"name": "相邻B桥", "capacity": 5000, "daily_vehicles": 100,
             "daily_buses": 10}, "ops", "traffic_ops")
        self.a = self.svc.register_bridge(
            {"name": "本桥A", "capacity": 1000, "daily_vehicles": 1000,
             "daily_buses": 100, "neighbor_id": self.b["id"]},
            "eng", "bridge_engineer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit(self, request_id="REQ-1", level="lane_close", actor="duty1",
                budget=10000, bridge=None):
        return self.svc.submit_notice(
            {"bridge_id": (bridge or self.a)["id"], "level": level,
             "reason": "主梁裂缝扩展", "request_id": request_id,
             "network_budget": budget}, actor, "duty_officer")

    def test_two_level_release_and_snapshot(self):
        notice = self._submit()
        self.assertEqual(notice["status"], "pending_engineer")
        self.assertTrue(notice["quota_held"])
        notice = self.svc.engineer_release(
            notice["id"], {"expected_version": notice["version"]},
            "engineer1", "bridge_engineer")
        self.assertEqual(notice["status"], "pending_supervisor")
        self.assertIsNone(notice["snapshot"])  # 未发布，无快照
        notice = self.svc.supervisor_release(
            notice["id"], {"expected_version": notice["version"]},
            "super1", "safety_supervisor")
        self.assertEqual(notice["status"], "restricted")
        self.assertTrue(notice["quota_held"])
        self.assertIsNotNone(notice["snapshot"])  # 已发布，保留原始快照
        self.assertEqual(notice["snapshot"]["published_by"], "super1")
        # 发布后对相邻桥产生绕行承载
        load = self.svc.get_diversion(notice["id"], "viewer")
        self.assertGreater(load["remaining_vehicles"], 0)
        self.assertEqual(load["status"], "active")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_restore_must_wait_for_offload(self):
        notice = self._two_level_released()
        with self.assertRaises(ConflictError) as ctx:
            self.svc.restore(notice["id"], {"expected_version": notice["version"]},
                             "engineer1", "bridge_engineer")
        self.assertIn("未卸载", str(ctx.exception))
        load = self.svc.get_diversion(notice["id"], "viewer")
        self.svc.offload(notice["id"],
                         {"vehicles": load["remaining_vehicles"],
                          "buses": load["remaining_buses"]},
                         "ops", "traffic_ops")
        restored = self.svc.restore(
            notice["id"], {"expected_version": notice["version"]},
            "engineer1", "bridge_engineer")
        self.assertEqual(restored["status"], "restored")
        self.assertFalse(restored["quota_held"])
        self.assertEqual(self.svc.get_bridge(self.a["id"], "viewer")["status"],
                         "normal")

    def test_concurrent_submit_first_wins_quota(self):
        first = self._submit("REQ-A")
        with self.assertRaises(QuotaConflict) as ctx:
            self._submit("REQ-B", actor="duty2")
        self.assertEqual(ctx.exception.holder, "duty1")
        self.assertEqual(ctx.exception.notice_id, first["id"])
        # 先通过者完成后额度释放，后者可再提交
        self.svc.add_context(
            self.a["id"], {"kind": "inspection", "detail": "解除"},
            "duty1", "duty_officer")
        second = self._submit("REQ-B", actor="duty2")
        self.assertEqual(second["created_by"], "duty2")

    def test_context_change_invalidates_pending_but_keeps_snapshot(self):
        notice = self._two_level_released()
        snapshot_before = notice["snapshot"]
        # 再提交一条未发布通告
        other_bridge = self.svc.register_bridge(
            {"name": "邻桥C", "capacity": 5000, "daily_vehicles": 10},
            "ops", "traffic_ops")
        pending = self.svc.submit_notice(
            {"bridge_id": other_bridge["id"], "level": "load_limit",
             "reason": "巡检待核", "request_id": "REQ-C",
             "network_budget": 10000}, "duty2", "duty_officer")
        # 气象变化：未发布通告失效重算；已发布通告保留原始快照
        result = self.svc.add_context(
            other_bridge["id"], {"kind": "weather", "detail": "台风蓝色预警"},
            "duty2", "duty_officer")
        self.assertIn(pending["id"], result["invalidated_notice_ids"])
        refreshed_pending = self.svc.get_notice(pending["id"], "viewer")
        self.assertEqual(refreshed_pending["status"], "invalidated")
        self.assertFalse(refreshed_pending["quota_held"])
        refreshed = self.svc.get_notice(notice["id"], "viewer")
        self.assertEqual(refreshed["status"], "restricted")
        self.assertEqual(refreshed["snapshot"], snapshot_before)

    def test_emergency_release_requires_evidence_and_reviewer(self):
        notice = self._submit()
        # 缺证据
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self.svc.emergency_release(
                notice["id"], {"expected_version": notice["version"],
                               "reviewer": "reviewer2"},
                "super1", "safety_supervisor")
        # 复核人不能是自己
        with self.assertRaises(ValidationError):
            self.svc.emergency_release(
                notice["id"], {"expected_version": notice["version"],
                               "evidence": "现场照片imgur", "reviewer": "super1"},
                "super1", "safety_supervisor")
        notice = self.svc.emergency_release(
            notice["id"], {"expected_version": notice["version"],
                           "evidence": "现场照片imgur", "reviewer": "reviewer2"},
            "super1", "safety_supervisor")
        self.assertEqual(notice["status"], "emergency_pending_review")
        self.assertTrue(notice["snapshot"]["emergency"])
        # 复核驳回：撤销发布并释放额度
        reviewed = self.svc.review_emergency(
            notice["id"], {"approved": False, "detail": "证据不足",
                           "expected_version": notice["version"]},
            "reviewer2", "safety_supervisor")
        self.assertEqual(reviewed["status"], "revoked")
        self.assertFalse(reviewed["quota_held"])
        self.assertEqual(self.svc.get_bridge(self.a["id"], "viewer")["status"],
                         "normal")

    def test_emergency_release_approved_becomes_restricted(self):
        notice = self._submit()
        notice = self.svc.emergency_release(
            notice["id"], {"expected_version": notice["version"],
                           "evidence": "视频v1", "reviewer": "reviewer2"},
            "super1", "safety_supervisor")
        reviewed = self.svc.review_emergency(
            notice["id"], {"approved": True, "detail": "证据确凿",
                           "expected_version": notice["version"]},
            "reviewer2", "safety_supervisor")
        self.assertEqual(reviewed["status"], "restricted")
        self.assertTrue(reviewed["quota_held"])

    def test_permission_denied_for_wrong_role(self):
        notice = self._submit()
        with self.assertRaises(PermissionDenied):
            self.svc.engineer_release(
                notice["id"], {"expected_version": notice["version"]},
                "duty1", "duty_officer")
        with self.assertRaises(PermissionDenied):
            self.svc.submit_notice(
                {"bridge_id": self.a["id"], "level": "load_limit",
                 "reason": "x", "request_id": "X", "network_budget": 10},
                "duty1", "viewer")

    def test_capacity_fail_blocks_supervisor_release(self):
        notice = self._submit(level="full_close", budget=5)  # 预算明显不足
        notice = self.svc.engineer_release(
            notice["id"], {"expected_version": notice["version"]},
            "engineer1", "bridge_engineer")
        with self.assertRaises(ConflictError):
            self.svc.supervisor_release(
                notice["id"], {"expected_version": notice["version"]},
                "super1", "safety_supervisor")

    def _two_level_released(self):
        notice = self._submit()
        notice = self.svc.engineer_release(
            notice["id"], {"expected_version": notice["version"]},
            "engineer1", "bridge_engineer")
        return self.svc.supervisor_release(
            notice["id"], {"expected_version": notice["version"]},
            "super1", "safety_supervisor")

    def _release_or_invalidate(self, notice):
        self.svc.add_context(
            notice["bridge_id"], {"kind": "inspection", "detail": "解除"},
            "duty1", "duty_officer")


if __name__ == "__main__":
    unittest.main()
