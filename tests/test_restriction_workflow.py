import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class RestrictionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        # 创建一座桥梁
        self.bridge = self.service.create_item(
            {"title": "跨河大桥", "description": "主桥结构监测", "severity": "warning",
             "quantity": 12, "threshold": 6, "external_ref": "BR-1"},
            "creator", "sensor_operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _make_notice(self, actor="duty1"):
        return self.service.create_restriction(
            self.bridge["id"], {"severity": "warning"}, actor, "duty_officer")

    def _approve_two_levels(self, notice_id):
        self.service.approve_notice(notice_id, {"comment": "工程师同意"},
                                    "eng1", "bridge_engineer")
        self.service.approve_notice(notice_id, {"comment": "监督员同意"},
                                    "sup1", "safety_supervisor")

    # ------------------------------------------------------------------
    # 规则1：值班员报预警 → 桥梁工程师 → 安全监督员两级放行
    # ------------------------------------------------------------------

    def test_two_level_approval_then_publish(self):
        notice = self._make_notice()
        self.assertEqual(notice["status"], "pending_approval")
        # 工程师审批
        notice = self.service.approve_notice(notice["id"], {"comment": "同意"},
                                             "eng1", "bridge_engineer")
        self.assertEqual(notice["status"], "engineer_approved")
        # 监督员审批
        notice = self.service.approve_notice(notice["id"], {"comment": "同意"},
                                             "sup1", "safety_supervisor")
        self.assertEqual(notice["status"], "supervisor_approved")
        # 发布
        notice = self.service.publish_notice(notice["id"], {"closure_load": 0},
                                              "sup1", "safety_supervisor")
        self.assertEqual(notice["status"], "published")
        self.assertIsNotNone(notice["snapshot"])
        self.assertIsNotNone(notice["published_at"])

    def test_cannot_publish_without_two_level_approval(self):
        notice = self._make_notice()
        with self.assertRaises(ConflictError):
            self.service.publish_notice(notice["id"], {"closure_load": 0},
                                        "sup1", "safety_supervisor")

    def test_wrong_role_cannot_approve(self):
        notice = self._make_notice()
        with self.assertRaises(PermissionDenied):
            self.service.approve_notice(notice["id"], {}, "attacker", "viewer")

    # ------------------------------------------------------------------
    # 规则2：封路占路网容量，公交绕行、救护通道、相邻桥梁限行一起核算
    # ------------------------------------------------------------------

    def test_network_capacity_check_passes(self):
        # 添加绕行路线
        self.service.add_detour_route(self.bridge["id"], {
            "kind": "bus", "name": "公交绕行线", "capacity": 100, "current_load": 20,
        }, "auth1", "traffic_authority")
        self.service.add_detour_route(self.bridge["id"], {
            "kind": "ambulance", "name": "救护通道", "capacity": 50, "current_load": 10,
        }, "auth1", "traffic_authority")
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        # closure_load=30 < 可用容量 (80+40=120)
        notice = self.service.publish_notice(notice["id"], {"closure_load": 30},
                                              "sup1", "safety_supervisor")
        self.assertEqual(notice["status"], "published")

    def test_network_capacity_check_fails_when_overloaded(self):
        self.service.add_detour_route(self.bridge["id"], {
            "kind": "bus", "name": "公交绕行线", "capacity": 10, "current_load": 10,
        }, "auth1", "traffic_authority")
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        with self.assertRaises(ConflictError):
            self.service.publish_notice(notice["id"], {"closure_load": 50},
                                        "sup1", "safety_supervisor")

    def test_adjacent_bridge_must_restrict_when_overloaded(self):
        self.service.add_detour_route(self.bridge["id"], {
            "kind": "adjacent_bridge", "name": "相邻桥A", "capacity": 20,
            "current_load": 15,
        }, "auth1", "traffic_authority")
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        # closure_load=30 > 相邻桥剩余容量 5
        with self.assertRaises(ConflictError):
            self.service.publish_notice(notice["id"], {"closure_load": 30},
                                        "sup1", "safety_supervisor")

    # ------------------------------------------------------------------
    # 规则3：相邻桥承接绕行流量时，原桥恢复申请要等下游卸载完成
    # ------------------------------------------------------------------

    def test_restore_blocked_until_downstream_unloaded(self):
        self.service.add_detour_route(self.bridge["id"], {
            "kind": "adjacent_bridge", "name": "相邻桥A", "capacity": 100,
            "current_load": 0,
        }, "auth1", "traffic_authority")
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        notice = self.service.publish_notice(notice["id"], {"closure_load": 50},
                                              "sup1", "safety_supervisor")
        self.assertEqual(notice["status"], "published")
        # 下游桥有绕行荷载（extra_load=50），恢复被阻止
        with self.assertRaises(ConflictError):
            self.service.restore_notice(notice["id"], {}, "eng1", "bridge_engineer")

    def test_restore_allowed_after_downstream_unloaded(self):
        # 无绕行影响（closure_load=0），可直接恢复
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        notice = self.service.publish_notice(notice["id"], {"closure_load": 0},
                                              "sup1", "safety_supervisor")
        notice = self.service.restore_notice(notice["id"], {}, "eng1", "bridge_engineer")
        self.assertEqual(notice["status"], "restored")

    # ------------------------------------------------------------------
    # 规则4：两名值班员同时提交同一座桥梁告警，先通过者占用发布额度，
    #         后到者看到被谁占用
    # ------------------------------------------------------------------

    def test_quota_first_come_first_served(self):
        notice1 = self._make_notice("duty1")
        notice2 = self._make_notice("duty2")
        self._approve_two_levels(notice1["id"])
        self._approve_two_levels(notice2["id"])
        # 先到者发布，占用额度
        published1 = self.service.publish_notice(notice1["id"], {"closure_load": 0},
                                                  "sup1", "safety_supervisor")
        self.assertEqual(published1["status"], "published")
        # 后到者发布，看到被谁占用
        with self.assertRaises(ConflictError) as ctx:
            self.service.publish_notice(notice2["id"], {"closure_load": 0},
                                        "sup2", "safety_supervisor")
        self.assertIn("sup1", str(ctx.exception))
        self.assertIn(notice1["id"].__str__(), str(ctx.exception))

    def test_quota_released_after_restore(self):
        notice1 = self._make_notice("duty1")
        notice2 = self._make_notice("duty2")
        self._approve_two_levels(notice1["id"])
        self._approve_two_levels(notice2["id"])
        self.service.publish_notice(notice1["id"], {"closure_load": 0},
                                    "sup1", "safety_supervisor")
        # 恢复后额度释放
        self.service.restore_notice(notice1["id"], {}, "eng1", "bridge_engineer")
        # 后到者可发布
        published2 = self.service.publish_notice(notice2["id"], {"closure_load": 0},
                                                  "sup2", "safety_supervisor")
        self.assertEqual(published2["status"], "published")

    def test_quota_status_shows_holder(self):
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        self.service.publish_notice(notice["id"], {"closure_load": 0},
                                    "sup1", "safety_supervisor")
        status = self.service.quota_status(self.bridge["id"], "viewer")
        self.assertTrue(status["occupied"])
        self.assertEqual(status["holder"]["actor"], "sup1")

    # ------------------------------------------------------------------
    # 规则5：气象、交通通告或巡检记录变化后，未发布的限行通告失效重算，
    #         已公布内容保留原始快照
    # ------------------------------------------------------------------

    def test_unpublished_notice_invalidated_on_record_change(self):
        notice = self._make_notice()
        self.assertEqual(notice["status"], "pending_approval")
        # 添加巡检记录（条件变化）
        self.service.add_record(self.bridge["id"], {
            "kind": "inspection", "detail": "支座病害发展", "status": "open",
        }, "recorder", "sensor_operator")
        # 未发布通告已失效
        notice = self.service.get_notice(notice["id"], "viewer")
        self.assertEqual(notice["status"], "invalidated")

    def test_published_notice_keeps_snapshot_on_change(self):
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        published = self.service.publish_notice(notice["id"], {"closure_load": 0},
                                                "sup1", "safety_supervisor")
        original_snapshot = published["snapshot"]
        # 条件变化
        self.service.add_record(self.bridge["id"], {
            "kind": "weather", "detail": "暴雨红色预警", "status": "open",
        }, "recorder", "sensor_operator")
        # 已公布内容保留原始快照
        notice = self.service.get_notice(notice["id"], "viewer")
        self.assertEqual(notice["status"], "published")
        self.assertEqual(notice["snapshot"], original_snapshot)

    # ------------------------------------------------------------------
    # 规则6：写入失败后从剩余步骤恢复，已占用额度只记一次
    # ------------------------------------------------------------------

    def test_recover_from_quota_step(self):
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        # 模拟额度占用后失败
        with self.assertRaises(RuntimeError):
            self.service.publish_notice(notice["id"], {"closure_load": 0},
                                        "sup1", "safety_supervisor",
                                        _fail_after="quota")
        # 额度已占用一次
        holder = self.repo.get_quota_holder(self.bridge["id"])
        self.assertIsNotNone(holder)
        self.assertEqual(holder["notice_id"], notice["id"])
        # 从剩余步骤恢复
        recovered = self.service.recover_notice(notice["id"], "sup1", "safety_supervisor")
        self.assertEqual(recovered["status"], "published")
        # 额度只记一次
        ledger = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM publish_quota_ledger WHERE notice_id=?",
            (notice["id"],)).fetchone()
        self.assertEqual(int(ledger["n"]), 1)

    def test_recover_from_snapshot_step(self):
        notice = self._make_notice()
        self._approve_two_levels(notice["id"])
        with self.assertRaises(RuntimeError):
            self.service.publish_notice(notice["id"], {"closure_load": 0},
                                        "sup1", "safety_supervisor",
                                        _fail_after="snapshot")
        recovered = self.service.recover_notice(notice["id"], "sup1", "safety_supervisor")
        self.assertEqual(recovered["status"], "published")
        self.assertIsNotNone(recovered["snapshot"])

    # ------------------------------------------------------------------
    # 规则7：安全监督员紧急放行必须绑定现场证据和复核人，
    #         复核不通过就撤销发布并释放额度
    # ------------------------------------------------------------------

    def test_emergency_publish_requires_evidence_and_reviewer(self):
        notice = self._make_notice()
        with self.assertRaises(ValidationError):
            self.service.emergency_publish(notice["id"], {
                "evidence": "现场照片",
            }, "sup1", "safety_supervisor")

    def test_emergency_publish_reviewer_cannot_be_self(self):
        notice = self._make_notice()
        with self.assertRaises(ValidationError):
            self.service.emergency_publish(notice["id"], {
                "evidence": "现场照片", "reviewer": "sup1",
            }, "sup1", "safety_supervisor")

    def test_emergency_publish_then_review_approved(self):
        notice = self._make_notice()
        published = self.service.emergency_publish(notice["id"], {
            "evidence": "现场照片", "reviewer": "rev1",
        }, "sup1", "safety_supervisor")
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["emergency"], 1)
        # 复核通过
        reviewed = self.service.review_emergency(notice["id"], {"approved": True},
                                                 "rev1", "reviewer")
        self.assertEqual(reviewed["status"], "published")

    def test_emergency_publish_then_review_rejected_revokes(self):
        notice = self._make_notice()
        self.service.emergency_publish(notice["id"], {
            "evidence": "现场照片", "reviewer": "rev1",
        }, "sup1", "safety_supervisor")
        # 复核不通过：撤销发布并释放额度
        reviewed = self.service.review_emergency(notice["id"], {"approved": False},
                                                 "rev1", "reviewer")
        self.assertEqual(reviewed["status"], "revoked")
        # 额度已释放
        holder = self.repo.get_quota_holder(self.bridge["id"])
        self.assertIsNone(holder)

    def test_only_safety_supervisor_can_emergency_publish(self):
        notice = self._make_notice()
        with self.assertRaises(PermissionDenied):
            self.service.emergency_publish(notice["id"], {
                "evidence": "现场照片", "reviewer": "rev1",
            }, "eng1", "bridge_engineer")


if __name__ == "__main__":
    unittest.main()
