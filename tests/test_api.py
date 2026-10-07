"""HTTP API 冒烟测试：真实起一个随机端口的服务，用 urllib 调用。"""
from __future__ import annotations



import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from talent_training.api import ApiHandler, _build_routes
from talent_training.service import Service


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = Service(db_path=":memory:", start_worker=False)
        ApiHandler.routes = _build_routes(cls.svc)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), ApiHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.svc.shutdown()
        cls.thread.join(timeout=3)

    def request(self, method: str, path: str, payload=None, actor="tester"):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-Actor": actor},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_full_flow_over_http(self):
        code, body = self.request("POST", "/api/providers",
                                  {"id": "p", "name": "学院", "region": "苏南"})
        self.assertEqual(code, 200, body)
        self.request("POST", "/api/enterprises",
                     {"id": "e", "name": "厂", "region": "苏南", "priority": 5})
        self.request("POST", "/api/students",
                     {"id": "a", "name": "甲", "home_region": "苏南",
                      "enterprise_id": "e", "skills": {"welding": 1}})
        code, body = self.request("POST", "/api/students",
                                  {"id": "a", "name": "甲", "home_region": "苏南"})
        self.assertEqual(code, 400)  # 重复学员
        self.request("POST", "/api/classes",
                     {"id": "k", "provider_id": "p", "skill_code": "welding",
                      "level": 3, "region": "苏南", "capacity": 2, "year": 2026})
        self.request("POST", "/api/quotas",
                     {"year": 2026, "region": "苏南", "skill_code": "welding", "total": 2})
        self.request("POST", "/api/needs",
                     {"id": "n", "enterprise_id": "e", "skill_code": "welding",
                      "target_level": 3, "allowed_regions": ["苏南"], "headcount": 1,
                      "year": 2026, "candidate_ids": ["a"]})
        code, body = self.request("POST", "/api/allocations/run", {"note": "接口分配"})
        self.assertEqual(code, 200)
        run_id = body["data"]["run_id"]
        self.assertEqual(body["data"]["stats"]["assigned"], 1)

        code, body = self.request("GET", f"/api/allocations/{run_id}")
        self.assertEqual(code, 200)
        decisions = body["data"]["decisions"]
        self.assertTrue(any("R2_no_duplicate" in d["trace_json"]
                            or "R1_need_valid" in d["trace_json"] for d in decisions))

        code, body = self.request("GET", "/api/audit?entity_type=allocation_run")
        self.assertEqual(code, 200)
        self.assertTrue(any(e["entity_id"] == run_id for e in body["data"]))

        code, body = self.request("POST", "/api/audit/verify")
        self.assertEqual(code, 200)
        self.assertTrue(body["data"]["ok"])

    def test_404_and_bad_json(self):
        code, _ = self.request("GET", "/api/students/does-not-exist")
        self.assertEqual(code, 404)
        code, _ = self.request("GET", "/api/nope")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
