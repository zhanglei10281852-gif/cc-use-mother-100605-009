"""服务门面：统一持有数据库连接、审计、锁与候补工作线程。

所有写操作都在同一把可重入锁内执行并提交，保证 HTTP 请求线程与
候补后台线程不会交叉写库；崩溃时事务回滚，已提交状态全部持久化。
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import allocator, audit as audit_mod, entities, operations, quota as quota_mod, waitlist
from .db import connect, init_db


class Service:
    def __init__(self, db_path: str = "data/talent.db", start_worker: bool = True,
                 poll_interval: float = 2.0):
        self.conn = connect(db_path)
        init_db(self.conn)
        self.lock = threading.RLock()
        self.audit = audit_mod.AuditLog(self.conn)
        self.worker = waitlist.WaitlistWorker(self.conn, self.audit, self.lock,
                                              poll_interval=poll_interval)
        self.db_path = db_path
        recovered = self.worker.recover()
        self._start_worker = start_worker
        if start_worker:
            self.worker.start()
        self.recovered_waitlist = recovered

    def shutdown(self) -> None:
        self.worker.stop()
        with self.lock:
            self.conn.commit()
            self.conn.close()

    # ------------------------------------------------- 包装：加锁 + 提交
    def _tx(self, fn, *args, **kwargs):
        with self.lock:
            try:
                result = fn(self.conn, self.audit, *args, **kwargs)
                self.conn.commit()
                return result
            except Exception:
                self.conn.rollback()
                raise

    # 机构 / 企业 / 学员 / 班级
    def create_provider(self, data, actor="admin"):
        return self._tx(entities.create_provider, data, actor)

    def create_enterprise(self, data, actor="admin"):
        return self._tx(entities.create_enterprise, data, actor)

    def create_student(self, data, actor="admin"):
        return self._tx(entities.create_student, data, actor)

    def create_class(self, data, actor="admin"):
        return self._tx(entities.create_class, data, actor)

    def create_need(self, data, actor="admin"):
        return self._tx(entities.create_need, data, actor)

    def import_batch(self, payload, actor="admin"):
        return self._tx(entities.import_batch, payload, actor)

    # 查询（无需事务包装，但加锁避免读到半成品）
    def list_providers(self):
        with self.lock:
            return entities.list_providers(self.conn)

    def list_enterprises(self):
        with self.lock:
            return entities.list_enterprises(self.conn)

    def list_students(self):
        with self.lock:
            return entities.list_students(self.conn)

    def list_classes(self, year=None):
        with self.lock:
            return entities.list_classes(self.conn, year)

    def list_needs(self, status=None):
        with self.lock:
            return entities.list_needs(self.conn, status)

    def get_student(self, sid):
        with self.lock:
            return entities.get_student(self.conn, sid)

    def get_class(self, cid):
        with self.lock:
            return entities.get_class(self.conn, cid)

    def get_need(self, nid):
        with self.lock:
            return entities.get_need(self.conn, nid)

    # 分配
    def run_allocation(self, actor="scheduler", note="", need_ids=None):
        result = self._tx(allocator.run_allocation, actor, note, need_ids)
        self.worker.wake()
        return result

    def get_run(self, run_id):
        with self.lock:
            return allocator.get_run(self.conn, run_id)

    def list_runs(self, limit=50):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, actor, note, status, stats_json, created_at "
                "FROM allocation_run ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            import json
            out = []
            for r in rows:
                d = dict(r)
                d["stats"] = json.loads(d.pop("stats_json"))
                out.append(d)
            return out

    # 配额
    def set_quota(self, year, region, skill_code, total, actor="admin"):
        result = self._tx(quota_mod.set_quota, year, region, skill_code, total, actor)
        self.worker.wake()  # 配额增加可能立即满足候补
        return result

    def list_quota(self, year=None):
        with self.lock:
            return quota_mod.list_quota(self.conn, year)

    def list_ledger(self, year=None, limit=200):
        with self.lock:
            return quota_mod.list_ledger(self.conn, year, limit)

    def carry_over(self, from_year, to_year, actor="admin"):
        return self._tx(quota_mod.carry_over, from_year, to_year, actor)

    # 生命周期操作
    def mark_attendance(self, enr_id, attended, actor="admin"):
        return self._tx(operations.mark_attendance, enr_id, attended, actor)

    def complete(self, enr_id, actor="admin"):
        return self._tx(operations.complete, enr_id, actor)

    def cancel_enrollment(self, enr_id, actor="admin", reason="学员退课"):
        result = self._tx(operations.cancel_enrollment, enr_id, actor, reason)
        self.worker.wake()
        return result

    def cancel_class(self, class_id, actor="admin", reason="课程临时取消"):
        result = self._tx(operations.cancel_class, class_id, actor, reason)
        self.worker.wake()
        return result

    def suspend_provider(self, provider_id, actor="admin", reason="机构停课"):
        result = self._tx(operations.suspend_provider, provider_id, actor, reason)
        self.worker.wake()
        return result

    def resume_provider(self, provider_id, actor="admin"):
        return self._tx(operations.resume_provider, provider_id, actor)

    def transfer_student(self, enr_id, target_class_id, actor="admin", reason="学员转班"):
        result = self._tx(operations.transfer_student, enr_id, target_class_id, actor, reason)
        self.worker.wake()
        return result

    def list_enrollments(self, class_id=None, student_id=None):
        with self.lock:
            return operations.list_enrollments(self.conn, class_id, student_id)

    def process_waitlist_now(self):
        with self.lock:
            try:
                result = self.worker.process_pending()
                self.conn.commit()
                return result
            except Exception:
                self.conn.rollback()
                raise

    def list_waitlist(self, status=None):
        with self.lock:
            return waitlist.list_waitlist(self.conn, status)

    # 审计
    def list_audit(self, limit=100, entity_type=None, entity_id=None):
        with self.lock:
            return self.audit.list(limit, entity_type, entity_id)

    def verify_audit(self):
        with self.lock:
            return self.audit.verify_chain()

    def list_imports(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM import_batch ORDER BY created_at DESC").fetchall()]
