"""持久化候补队列处理器。

- 候补条目全部落库（waitlist_entry），状态机 pending -> processing -> placed / pending(重试)。
- 后台工作线程按企业优先级、登记时间顺序轮询；有名额释放（课程取消/转班/配额调整）
  后可被 wake() 立即唤醒重试。
- 服务重启时，崩溃残留在 processing 的条目会被复位为 pending 后继续处理，
  不会漏掉任何一个候补学员。
"""
from __future__ import annotations

import threading
import time

from . import entities, quota as quota_mod
from .util import now_iso


class WaitlistWorker:
    def __init__(self, conn, audit, lock: threading.RLock, poll_interval: float = 2.0):
        self._conn = conn
        self._audit = audit
        self._lock = lock
        self._poll = poll_interval
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期
    def recover(self) -> int:
        """启动恢复：把崩溃残留的 processing 复位为 pending。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE waitlist_entry SET status='pending', updated_at=? "
                "WHERE status='processing'",
                (now_iso(),),
            )
            n = cur.rowcount
            if n:
                self._audit.append("system", "waitlist.recover", "waitlist", None,
                                   {"reset_processing": n})
            self._conn.commit()
            return n

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="waitlist-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake_event.set()

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_pending()
            except Exception as exc:  # 工作线程不能因单次异常退出
                with self._lock:
                    self._conn.rollback()
                print(f"[waitlist] 处理异常: {exc!r}")
            self._wake_event.wait(timeout=self._poll)
            self._wake_event.clear()

    def process_pending(self, limit: int = 50) -> dict:
        """处理一批候补条目；HTTP 接口与后台线程都可调用。

        每次只处理一批：仍不满足条件的条目回到 pending，等下一轮轮询或
        名额释放时被唤醒，避免对持续失败条目忙等。
        """
        stats = {"placed": 0, "still_waiting": 0}
        with self._lock:
            batch = self._conn.execute(
                """SELECT * FROM waitlist_entry
                   WHERE status='pending'
                   ORDER BY priority DESC, created_at ASC LIMIT ?""",
                (limit,),
            ).fetchall()
            for row in batch:
                before = dict(row)
                self._process_one(before)
            # 统计这批结果
            ids = [r["id"] for r in batch]
            if ids:
                marks = ",".join("?" * len(ids))
                stats["placed"] = self._conn.execute(
                    f"SELECT COUNT(*) AS c FROM waitlist_entry WHERE status='placed' AND id IN ({marks})",
                    ids,
                ).fetchone()["c"]
                stats["still_waiting"] = self._conn.execute(
                    f"SELECT COUNT(*) AS c FROM waitlist_entry WHERE status='pending' AND id IN ({marks})",
                    ids,
                ).fetchone()["c"]
            self._conn.commit()
        return stats

    # ------------------------------------------------------------ 单条处理
    def _claim(self, entry_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE waitlist_entry SET status='processing', attempts=attempts+1, "
            "updated_at=? WHERE id=? AND status='pending'",
            (now_iso(), entry_id),
        )
        return cur.rowcount == 1

    def _process_one(self, entry: dict) -> None:
        if not self._claim(entry["id"]):
            return
        need = entities.get_need(self._conn, entry["need_id"])
        if not need:
            self._fail(entry["id"], "关联需求不存在", dead=True)
            return
        student = entities.get_student(self._conn, entry["student_id"])
        if not student:
            self._fail(entry["id"], "学员不存在", dead=True)
            return

        # 候补期间学员可能已在其他安排中结业/登记
        from .allocator import existing_placement
        if existing_placement(self._conn, student["id"], need["skill_code"], need["year"]):
            self._conn.execute(
                "UPDATE waitlist_entry SET status='cancelled', last_error=?, updated_at=? WHERE id=?",
                ("候补期间已存在有效安排", now_iso(), entry["id"]),
            )
            return

        cls = self._find_class(need, student)
        if not cls:
            self._retry(entry["id"], "暂无可接收班级（容量或配额不足）")
            return

        enr_id = entry["enrollment_id"]
        self._conn.execute(
            "UPDATE enrollment SET class_id=?, status='registered', reason=NULL, "
            "updated_at=? WHERE id=?",
            (cls["id"], now_iso(), enr_id),
        )
        quota_mod.consume(self._conn, need["year"], cls["region"], need["skill_code"],
                          1, enr_id, actor="waitlist")
        self._conn.execute(
            "UPDATE waitlist_entry SET status='placed', last_error=NULL, updated_at=? WHERE id=?",
            (now_iso(), entry["id"]),
        )
        self._audit.append("waitlist", "waitlist.placed", "enrollment", enr_id,
                           {"waitlist_id": entry["id"], "class_id": cls["id"],
                            "student_id": student["id"], "attempts": entry["attempts"] + 1})

    def _find_class(self, need: dict, student: dict) -> dict | None:
        """候补匹配：技能/等级一致、地域允许、机构在营、班级开放、有名额、有配额。"""
        rows = self._conn.execute(
            "SELECT * FROM class_session WHERE skill_code=? AND level=? AND year=? AND status='open' "
            "ORDER BY capacity DESC",
            (need["skill_code"], need["target_level"], need["year"]),
        ).fetchall()
        from .allocator import class_remaining
        for crow in rows:
            cls = dict(crow)
            if cls["region"] not in need["allowed_regions"]:
                continue
            provider = entities.get_provider(self._conn, cls["provider_id"])
            if not provider["active"]:
                continue
            if class_remaining(self._conn, cls) <= 0:
                continue
            q = quota_mod.get_quota(self._conn, need["year"], cls["region"], need["skill_code"])
            if q is None or q["available"] <= 0:
                continue
            return cls
        return None

    def _retry(self, entry_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE waitlist_entry SET status='pending', last_error=?, updated_at=? WHERE id=?",
            (error, now_iso(), entry_id),
        )

    def _fail(self, entry_id: str, error: str, dead: bool) -> None:
        self._conn.execute(
            "UPDATE waitlist_entry SET status=?, last_error=?, updated_at=? WHERE id=?",
            ("dead" if dead else "pending", error, now_iso(), entry_id),
        )


def list_waitlist(conn, status: str | None = None) -> list[dict]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM waitlist_entry ORDER BY status, priority DESC, created_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM waitlist_entry WHERE status=? ORDER BY priority DESC, created_at",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]
