import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service
from http.server import ThreadingHTTPServer


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Repository(str(Path(cls.tmp.name) / "test.db"))
        cls.service = Service(cls.repo)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            cls.service, str(Path(__file__).resolve().parent.parent / "static")))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.repo.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, actor="tester", role="viewer"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"X-Actor": actor, "X-Role": role}
        if payload:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_full_flow_over_http(self):
        status, health = self.call("GET", "/health")
        self.assertEqual((status, health["status"]), (200, "ok"))

        status, b = self.call("POST", "/api/bridges",
                              {"name": "相邻桥X", "capacity": 5000},
                              actor="ops", role="traffic_ops")
        self.assertEqual(status, 201)
        status, a = self.call("POST", "/api/bridges",
                              {"name": "本桥Y", "capacity": 1000,
                               "daily_vehicles": 800, "daily_buses": 40,
                               "neighbor_id": b["id"]},
                              actor="eng", role="bridge_engineer")
        self.assertEqual(status, 201)

        body = {"bridge_id": a["id"], "level": "lane_close", "reason": "横向位移",
                "request_id": "HTTP-1", "network_budget": 10000}
        status, first = self.call("POST", "/api/notices", body,
                                  actor="duty1", role="duty_officer")
        self.assertEqual(status, 201)

        # 第二名值班员并发提交同一桥梁：看到额度被谁占用
        body2 = dict(body, request_id="HTTP-2")
        status, err = self.call("POST", "/api/notices", body2,
                                actor="duty2", role="duty_officer")
        self.assertEqual(status, 409)
        self.assertEqual(err["holder"], "duty1")
        self.assertEqual(err["notice_id"], first["id"])

        status, n = self.call(
            "POST", f"/api/notices/{first['id']}/engineer-release",
            {"expected_version": first["version"]},
            actor="eng1", role="bridge_engineer")
        self.assertEqual(status, 200)
        status, n = self.call(
            "POST", f"/api/notices/{n['id']}/supervisor-release",
            {"expected_version": n["version"]},
            actor="sup1", role="safety_supervisor")
        self.assertEqual(status, 200)
        self.assertEqual(n["status"], "restricted")
        self.assertIn("snapshot", n)

        # 越权
        status, err = self.call("GET", "/api/audit", actor="x", role="duty_officer")
        self.assertEqual(status, 403)

        status, events = self.call("GET", "/api/audit", actor="e",
                                   role="bridge_engineer")
        self.assertEqual(status, 200)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
