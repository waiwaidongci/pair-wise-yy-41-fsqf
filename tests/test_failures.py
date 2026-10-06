import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, QuotaConflict
from src.repository import Repository
from src.service import Service


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.svc = Service(self.repo)
        self.bridge = self.svc.register_bridge(
            {"name": "测试桥", "capacity": 5000, "daily_vehicles": 1000,
             "daily_buses": 50}, "eng", "bridge_engineer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _payload(self, request_id="FAIL-1", budget=10000):
        return {"bridge_id": self.bridge["id"], "level": "lane_close",
                "reason": "异常下挠", "request_id": request_id,
                "network_budget": budget}

    def test_version_conflict(self):
        notice = self.svc.submit_notice(self._payload(), "duty1", "duty_officer")
        self.svc.engineer_release(notice["id"], {"expected_version": 1},
                                  "eng1", "bridge_engineer")
        with self.assertRaises(ConflictError):
            self.svc.engineer_release(notice["id"], {"expected_version": 1},
                                      "eng1", "bridge_engineer")

    def test_resume_after_insert_notice_failure_keeps_quota_once(self):
        # 占额度成功后，建通告时写入失败
        self.repo.inject_fault("insert_notice", 1)
        with self.assertRaises(RuntimeError):
            self.svc.submit_notice(self._payload(), "duty1", "duty_officer")
        # 额度已被本次操作占用；其他值班员不能抢占
        with self.assertRaises(QuotaConflict) as ctx:
            self.svc.submit_notice(self._payload("FAIL-2"), "duty2",
                                   "duty_officer")
        self.assertEqual(ctx.exception.holder, "duty1")
        # 用同一 request_id 从剩余步骤恢复
        notice = self.svc.submit_notice(self._payload(), "duty1", "duty_officer")
        self.assertEqual(notice["status"], "pending_engineer")
        # 恢复后审计只写一次
        submits = [e for e in self.repo.list_audit(notice["id"])
                   if e["action"] == "submit_notice"]
        self.assertEqual(len(submits), 1)
        # 额度只记一次
        rows = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM publish_quota WHERE bridge_id=?",
            (self.bridge["id"],)).fetchone()
        self.assertEqual(rows["n"], 1)
        notices = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM notices WHERE request_id='FAIL-1'").fetchone()
        self.assertEqual(notices["n"], 1)

    def test_resume_after_publish_audit_failure(self):
        notice = self.svc.submit_notice(self._payload(), "duty1", "duty_officer")
        notice = self.svc.engineer_release(
            notice["id"], {"expected_version": notice["version"]}, "eng1",
            "bridge_engineer")
        version = notice["version"]
        # 发布成功后、审计写入失败
        self.repo.inject_fault("audit", 1)
        with self.assertRaises(RuntimeError):
            self.svc.supervisor_release(
                notice["id"], {"expected_version": version}, "sup1",
                "safety_supervisor")
        # 已发布状态不重复写；重试（同版本）安全
        retried = self.svc.supervisor_release(
            notice["id"], {"expected_version": version}, "sup1",
            "safety_supervisor")
        self.assertEqual(retried["status"], "restricted")
        self.assertIsNotNone(retried["snapshot"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_duplicate_request_id_is_idempotent(self):
        first = self.svc.submit_notice(self._payload(), "duty1", "duty_officer")
        # 正常情况下额度已占用；先失效释放，再用同一 request_id 重提也不应产生重复
        self.svc.add_context(self.bridge["id"],
                             {"kind": "weather", "detail": "情况变化"},
                             "duty1", "duty_officer")
        second = self.svc.submit_notice(self._payload(), "duty1", "duty_officer")
        self.assertEqual(second["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
