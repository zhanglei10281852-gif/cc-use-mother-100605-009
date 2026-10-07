"""端到端业务测试：直接驱动 Service 门面，覆盖全部核心业务规则。"""
from __future__ import annotations

import unittest

from talent_training.service import Service


def make_service() -> Service:
    return Service(db_path=":memory:", start_worker=False)


def base_world(svc: Service, quota: int = 5, class_capacity: int = 5) -> None:
    svc.create_provider({"id": "p", "name": "技师学院", "region": "苏南"})
    svc.create_enterprise({"id": "e1", "name": "紧缺工厂", "region": "苏南", "priority": 100})
    svc.create_enterprise({"id": "e2", "name": "普通工厂", "region": "苏南", "priority": 1})
    svc.create_student({"id": "a", "name": "甲", "home_region": "苏南",
                        "enterprise_id": "e1", "skills": {"welding": 1}})
    svc.create_student({"id": "b", "name": "乙", "home_region": "苏南",
                        "enterprise_id": "e1", "skills": {"welding": 2}})
    svc.create_student({"id": "c", "name": "丙", "home_region": "苏北",
                        "enterprise_id": "e2", "skills": {"welding": 0}})
    svc.create_class({"id": "k", "provider_id": "p", "skill_code": "welding",
                      "level": 3, "region": "苏南", "capacity": class_capacity, "year": 2026})
    svc.set_quota(2026, "苏南", "welding", quota)


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.shutdown()

    def test_duplicate_student_across_needs_is_placed_only_once(self):
        base_world(self.svc)
        # 学员 a 同时出现在两个需求里
        self.svc.create_need({"id": "n1", "enterprise_id": "e1", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 1, "year": 2026, "candidate_ids": ["a", "b"]})
        self.svc.create_need({"id": "n2", "enterprise_id": "e2", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 1, "year": 2026, "candidate_ids": ["a", "c"]})
        run = self.svc.run_allocation()
        detail = self.svc.get_run(run["run_id"])
        results = {(d["need_id"], d["student_id"]): d["result"] for d in detail["decisions"]}
        self.assertEqual(results[("n1", "a")], "assigned")
        # 第二次出现 a 必须命中 R2 拒绝，而不是重复占座
        self.assertEqual(results[("n2", "a")], "rejected")
        n2_a = next(d for d in detail["decisions"]
                    if d["need_id"] == "n2" and d["student_id"] == "a")
        self.assertIn("R2_no_duplicate", n2_a["trace_json"])
        active = [e for e in self.svc.list_enrollments(student_id="a")
                  if e["status"] in ("registered", "attended", "completed")]
        self.assertEqual(len(active), 1)

    def test_priority_orders_enterprises(self):
        # 容量和配额都只有 1：高优先级企业应先拿到
        base_world(self.svc, quota=1, class_capacity=1)
        self.svc.create_student({"id": "c2", "name": "丙二", "home_region": "苏南",
                                 "enterprise_id": "e2", "skills": {"welding": 2}})
        self.svc.create_need({"id": "n_low", "enterprise_id": "e2", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 1, "year": 2026, "candidate_ids": ["c2"]})
        self.svc.create_need({"id": "n_hi", "enterprise_id": "e1", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 1, "year": 2026, "candidate_ids": ["a"]})
        run = self.svc.run_allocation()
        detail = self.svc.get_run(run["run_id"])
        order = [d["need_id"] for d in detail["decisions"]]
        self.assertEqual(order[0], "n_hi")  # 高优先级先处理
        results = {(d["need_id"], d["student_id"]): d["result"] for d in detail["decisions"]}
        self.assertEqual(results[("n_hi", "a")], "assigned")
        self.assertEqual(results[("n_low", "c2")], "waitlisted")  # 名额被高优先级占用

    def test_region_constraint_blocks_class(self):
        base_world(self.svc)
        # 用一名技能已达 2 级的学员，确保先通过 R3，再由 R5 地域规则拒绝
        self.svc.create_student({"id": "r", "name": "地域生", "home_region": "苏北",
                                 "enterprise_id": "e2", "skills": {"welding": 2}})
        self.svc.create_need({"id": "n", "enterprise_id": "e2", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏北"],
                              "headcount": 1, "year": 2026, "candidate_ids": ["r"]})
        run = self.svc.run_allocation()
        detail = self.svc.get_run(run["run_id"])
        dec = detail["decisions"][0]
        self.assertEqual(dec["result"], "rejected")
        self.assertIn("R5_region", dec["trace_json"])

    def test_skill_gap_too_large_rejected_and_already_qualified_rejected(self):
        base_world(self.svc)
        self.svc.create_student({"id": "d", "name": "丁", "home_region": "苏南",
                                 "enterprise_id": "e1", "skills": {"welding": 0}})
        self.svc.create_student({"id": "e", "name": "戊", "home_region": "苏南",
                                 "enterprise_id": "e1", "skills": {"welding": 5}})
        self.svc.create_need({"id": "n", "enterprise_id": "e1", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 2, "year": 2026, "candidate_ids": ["d", "e"]})
        run = self.svc.run_allocation()
        detail = self.svc.get_run(run["run_id"])
        by_student = {d["student_id"]: d for d in detail["decisions"]}
        self.assertIn("R3_skill_prerequisite", by_student["d"]["trace_json"])
        self.assertIn("无需培训", by_student["e"]["reason"])

    def test_capacity_and_quota_produce_waitlist(self):
        base_world(self.svc, quota=1, class_capacity=1)
        self.svc.create_need({"id": "n", "enterprise_id": "e1", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 2, "year": 2026, "candidate_ids": ["a", "b"]})
        run = self.svc.run_allocation()
        self.assertEqual(run["stats"]["assigned"], 1)
        self.assertEqual(run["stats"]["waitlisted"], 1)
        self.assertEqual(len(self.svc.list_waitlist(status="pending")), 1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        base_world(self.svc, quota=2, class_capacity=2)
        self.svc.create_need({"id": "n", "enterprise_id": "e1", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 2, "year": 2026, "candidate_ids": ["a", "b"]})
        self.svc.run_allocation()

    def tearDown(self):
        self.svc.shutdown()

    def test_attendance_and_completion_upgrades_skill(self):
        enr = next(e for e in self.svc.list_enrollments(student_id="a")
                   if e["status"] == "registered")
        self.svc.mark_attendance(enr["id"], True)
        self.svc.complete(enr["id"])
        self.assertEqual(self.svc.get_student("a")["skills"]["welding"], 3)

    def test_illegal_transition_rejected_properly(self):
        enr = next(e for e in self.svc.list_enrollments(student_id="a")
                   if e["status"] == "registered")
        with self.assertRaises(ValueError):
            self.svc.complete(enr["id"])  # registered 不能直接结业

    def test_cancel_class_releases_quota_and_requeues(self):
        q_before = self.svc.list_quota(2026)[0]
        self.assertEqual(q_before["used"], 2)
        # 开设一个更大的新班并追加配额，供候补消化
        self.svc.create_class({"id": "k2", "provider_id": "p", "skill_code": "welding",
                               "level": 3, "region": "苏南", "capacity": 5, "year": 2026})
        self.svc.set_quota(2026, "苏南", "welding", 5)
        self.svc.cancel_class("k", reason="厂房检修")
        q_after_cancel = next(q for q in self.svc.list_quota(2026))
        self.assertEqual(q_after_cancel["used"], 0)  # 两个名额全部回收
        result = self.svc.process_waitlist_now()
        self.assertEqual(result["placed"], 2)
        self.assertEqual(self.svc.list_quota(2026)[0]["used"], 2)

    def test_transfer_keeps_history_chain(self):
        self.svc.create_provider({"id": "p2", "name": "第二学院", "region": "苏南"})
        self.svc.create_class({"id": "k2", "provider_id": "p2", "skill_code": "welding",
                               "level": 3, "region": "苏南", "capacity": 3, "year": 2026})
        enr = next(e for e in self.svc.list_enrollments(student_id="a")
                   if e["status"] == "registered")
        result = self.svc.transfer_student(enr["id"], "k2", reason="就近入学")
        old = next(e for e in self.svc.list_enrollments(student_id="a")
                   if e["id"] == enr["id"])
        new = next(e for e in self.svc.list_enrollments(student_id="a")
                   if e["id"] == result["new_enrollment_id"])
        self.assertEqual(old["status"], "transferred")
        self.assertEqual(new["status"], "registered")
        self.assertEqual(new["class_id"], "k2")
        self.assertEqual(new["transferred_from"], old["id"])
        # 台账：一放一收，用量净值不变
        self.assertEqual(self.svc.list_quota(2026)[0]["used"], 2)

    def test_suspend_provider_releases_all_classes(self):
        result = self.svc.suspend_provider("p", reason="资质整改")
        self.assertEqual(result["seats_released"], 2)
        self.assertFalse(self.svc.list_providers()[0]["active"])
        with self.assertRaises(ValueError):
            self.svc.create_class({"id": "k3", "provider_id": "p", "skill_code": "welding",
                                   "level": 3, "region": "苏南", "capacity": 2, "year": 2026})


class QuotaAndImportTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.shutdown()

    def test_import_deduplicates_students_and_needs(self):
        payload = {
            "students": [
                {"id": "a", "name": "甲", "home_region": "苏南", "skills": {"welding": 1}},
                {"id": "a", "name": "甲", "home_region": "苏南", "skills": {"welding": 1}},
            ],
            "needs": [],
        }
        self.svc.create_provider({"id": "p", "name": "学院", "region": "苏南"})
        self.svc.create_enterprise({"id": "e1", "name": "厂", "region": "苏南", "priority": 1})
        # 企业需要先存在；把企业放入第一批之外先建好
        result = self.svc.import_batch(payload)
        self.assertEqual(result["new_students"], 1)
        self.assertEqual(result["duplicate_students"], 1)
        # 再来同样一批：学员全部识别为重复
        again = self.svc.import_batch(payload)
        self.assertEqual(again["new_students"], 0)
        self.assertEqual(again["duplicate_students"], 2)

    def test_need_idempotency_by_business_fingerprint(self):
        base_world(self.svc)
        data = {"enterprise_id": "e1", "skill_code": "welding", "target_level": 3,
                "allowed_regions": ["苏南"], "headcount": 1, "year": 2026,
                "candidate_ids": ["a"]}
        r1 = self.svc.create_need(dict(data, id="n1"))
        r2 = self.svc.create_need(dict(data, id="n2"))  # 同业务指纹
        self.assertFalse(r1["duplicate"])
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r2["id"], r1["id"])

    def test_carry_over_is_once_and_reflects_unused(self):
        base_world(self.svc, quota=10)
        self.svc.create_need({"id": "n", "enterprise_id": "e1", "skill_code": "welding",
                              "target_level": 3, "allowed_regions": ["苏南"],
                              "headcount": 2, "year": 2026, "candidate_ids": ["a", "b"]})
        self.svc.run_allocation()  # 用掉 2
        result = self.svc.carry_over(2026, 2027)
        carried = [i for i in result["items"] if i["carried"]]
        self.assertEqual(carried[0]["carried"], 8)
        q2027 = self.svc.list_quota(2027)[0]
        self.assertEqual(q2027["carry_in"], 8)
        self.assertEqual(q2027["available"], 8)
        with self.assertRaises(ValueError):
            self.svc.carry_over(2026, 2027)  # 不可重复结转

    def test_audit_chain_detects_tampering(self):
        base_world(self.svc)
        self.assertTrue(self.svc.verify_audit()["ok"])
        # 直接篡改库中一条审计
        with self.svc.lock:
            row = self.svc.conn.execute("SELECT seq FROM audit_log ORDER BY seq LIMIT 1").fetchone()
            self.svc.conn.execute("UPDATE audit_log SET action='hacked' WHERE seq=?", (row["seq"],))
            self.svc.conn.commit()
        verdict = self.svc.verify_audit()
        self.assertFalse(verdict["ok"])


class RecoveryTests(unittest.TestCase):
    def test_interrupted_waitlist_resumes_after_restart(self):
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "recover.db")
        svc = Service(db_path=path, start_worker=False)
        base_world(svc, quota=1, class_capacity=1)
        svc.create_need({"id": "n", "enterprise_id": "e1", "skill_code": "welding",
                         "target_level": 3, "allowed_regions": ["苏南"],
                         "headcount": 2, "year": 2026, "candidate_ids": ["a", "b"]})
        svc.run_allocation()
        self.assertEqual(len(svc.list_waitlist(status="pending")), 1)
        # 模拟处理中崩溃：把 pending 改成 processing
        with svc.lock:
            svc.conn.execute("UPDATE waitlist_entry SET status='processing'")
            svc.conn.commit()
        svc.shutdown()

        # 重启：processing 必须被复位
        svc2 = Service(db_path=path, start_worker=False)
        self.assertEqual(svc2.recovered_waitlist, 1)
        self.assertEqual(len(svc2.list_waitlist(status="pending")), 1)
        # 扩容后候补可以继续完成
        svc2.create_class({"id": "k2", "provider_id": "p", "skill_code": "welding",
                           "level": 3, "region": "苏南", "capacity": 3, "year": 2026})
        svc2.set_quota(2026, "苏南", "welding", 5)
        result = svc2.process_waitlist_now()
        self.assertEqual(result["placed"], 1)
        svc2.shutdown()


if __name__ == "__main__":
    unittest.main()
