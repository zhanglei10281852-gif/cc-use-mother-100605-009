"""报名生命周期：出勤、结业、退课、转班、机构停课/班级取消与名额释放。

所有状态迁移都校验合法性、同步维护年度配额台账，并写审计日志，
历史 enrollment 记录永不物理删除——转班通过 transferred_from 串联，
取消保留 reason，保证任何一次名额变动可追溯。
"""
from __future__ import annotations

from . import entities, quota as quota_mod
from .util import now_iso

# 报名状态机
TRANSITIONS = {
    "waitlisted": {"registered", "cancelled"},
    "registered": {"attended", "cancelled", "transferred", "no_show"},
    "attended": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
    "transferred": set(),
    "no_show": set(),
}

SEAT_HELD = ("registered", "attended", "completed")


def get_enrollment(conn, enr_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM enrollment WHERE id=?", (enr_id,)).fetchone()
    return dict(row) if row else None


def list_enrollments(conn, class_id: str | None = None, student_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM enrollment"
    clauses, params = [], []
    if class_id:
        clauses.append("class_id=?")
        params.append(class_id)
    if student_id:
        clauses.append("student_id=?")
        params.append(student_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _transition(conn, audit, enr_id: str, target: str, actor: str,
                reason: str | None = None, **extra) -> dict:
    enr = get_enrollment(conn, enr_id)
    if not enr:
        raise ValueError(f"报名记录 {enr_id} 不存在")
    if target not in TRANSITIONS[enr["status"]]:
        raise ValueError(f"报名状态不能从 {enr['status']} 迁移到 {target}")
    conn.execute(
        "UPDATE enrollment SET status=?, reason=COALESCE(?, reason), updated_at=? WHERE id=?",
        (target, reason, now_iso(), enr_id),
    )
    audit.append(actor, f"enrollment.{target}", "enrollment", enr_id,
                 {"from": enr["status"], "class_id": enr["class_id"],
                  "student_id": enr["student_id"], "reason": reason, **extra})
    return get_enrollment(conn, enr_id)


def _release_seat(conn, audit, enr: dict, actor: str, reason: str) -> None:
    """归还一个已占用的年度名额（registered/attended 都曾消耗配额，各回收一次）。"""
    if enr["status"] not in SEAT_HELD or not enr["class_id"]:
        return
    cls = entities.get_class(conn, enr["class_id"])
    quota_mod.release(conn, enr["year"], cls["region"], enr["skill_code"], 1,
                      ref_id=enr["id"], actor=actor, reason=reason)


def mark_attendance(conn, audit, enr_id: str, attended: bool, actor: str = "admin") -> dict:
    target = "attended" if attended else "no_show"
    return _transition(conn, audit, enr_id, target, actor,
                       reason="出勤登记" if attended else "缺勤登记")


def complete(conn, audit, enr_id: str, actor: str = "admin") -> dict:
    enr = _transition(conn, audit, enr_id, "completed", actor, reason="结业")
    # 结业后更新学员能力档案等级
    row = conn.execute(
        "SELECT n.target_level, n.skill_code, s.skills_json, s.id AS sid "
        "FROM enrollment e JOIN training_need n ON e.need_id=n.id "
        "JOIN student s ON e.student_id=s.id WHERE e.id=?",
        (enr_id,),
    ).fetchone()
    import json
    skills = json.loads(row["skills_json"])
    if int(skills.get(row["skill_code"], 0)) < row["target_level"]:
        skills[row["skill_code"]] = row["target_level"]
        conn.execute("UPDATE student SET skills_json=? WHERE id=?",
                     (json.dumps(skills, ensure_ascii=False, sort_keys=True), row["sid"]))
        audit.append(actor, "student.skill_upgraded", "student", row["sid"],
                     {"skill_code": row["skill_code"], "level": row["target_level"],
                      "via_enrollment": enr_id})
    return enr


def cancel_enrollment(conn, audit, enr_id: str, actor: str = "admin",
                      reason: str = "学员退课") -> dict:
    enr = get_enrollment(conn, enr_id)
    held_class_id = enr["class_id"]
    _release_seat(conn, audit, enr, actor, reason)
    result = _transition(conn, audit, enr_id, "cancelled", actor, reason=reason)
    result["freed_class_id"] = held_class_id
    return result


def cancel_class(conn, audit, class_id: str, actor: str = "admin",
                 reason: str = "课程临时取消") -> dict:
    """班级取消：释放所有在籍名额，学员回到候补队列等待重新安排。"""
    cls = entities.get_class(conn, class_id)
    if not cls:
        raise ValueError(f"班级 {class_id} 不存在")
    if cls["status"] in ("closed",):
        raise ValueError("班级已关闭")
    affected = []
    rows = conn.execute(
        "SELECT * FROM enrollment WHERE class_id=? AND status IN ('registered','attended')",
        (class_id,),
    ).fetchall()
    for row in rows:
        enr = dict(row)
        _release_seat(conn, audit, enr, actor, f"班级取消：{reason}")
        conn.execute(
            "UPDATE enrollment SET status='cancelled', reason=?, class_id=NULL, updated_at=? WHERE id=?",
            (f"班级取消：{reason}", now_iso(), enr["id"]),
        )
        _requeue(conn, enr, actor)
        affected.append(enr["id"])
    conn.execute("UPDATE class_session SET status='closed' WHERE id=?", (class_id,))
    audit.append(actor, "class.cancel", "class", class_id,
                 {"reason": reason, "released_enrollments": affected})
    return {"class_id": class_id, "released": len(affected), "enrollment_ids": affected}


def suspend_provider(conn, audit, provider_id: str, actor: str = "admin",
                     reason: str = "机构停课") -> dict:
    """机构停课：名下开放班级全部暂停，在籍名额回收、学员转候补。"""
    provider = entities.get_provider(conn, provider_id)
    if not provider:
        raise ValueError(f"机构 {provider_id} 不存在")
    conn.execute("UPDATE provider SET active=0 WHERE id=?", (provider_id,))
    classes = conn.execute(
        "SELECT id FROM class_session WHERE provider_id=? AND status='open'", (provider_id,)
    ).fetchall()
    released_total = 0
    for crow in classes:
        cid = crow["id"]
        result = cancel_class(conn, audit, cid, actor=actor,
                              reason=f"机构停课：{reason}")
        conn.execute("UPDATE class_session SET status='suspended' WHERE id=?", (cid,))
        released_total += result["released"]
    audit.append(actor, "provider.suspend", "provider", provider_id,
                 {"reason": reason, "classes_suspended": len(classes),
                  "seats_released": released_total})
    return {"provider_id": provider_id, "classes_suspended": len(classes),
            "seats_released": released_total}


def resume_provider(conn, audit, provider_id: str, actor: str = "admin") -> dict:
    provider = entities.get_provider(conn, provider_id)
    if not provider:
        raise ValueError(f"机构 {provider_id} 不存在")
    conn.execute("UPDATE provider SET active=1 WHERE id=?", (provider_id,))
    audit.append(actor, "provider.resume", "provider", provider_id, {})
    return entities.get_provider(conn, provider_id)


def transfer_student(conn, audit, enr_id: str, target_class_id: str,
                     actor: str = "admin", reason: str = "学员转班") -> dict:
    """学员转班：旧班释放、新班占用，两条 enrollment 通过 transferred_from 串联。"""
    old = get_enrollment(conn, enr_id)
    if not old:
        raise ValueError(f"报名记录 {enr_id} 不存在")
    if old["status"] != "registered":
        raise ValueError(f"仅 registered 状态可以转班，当前 {old['status']}")
    need = entities.get_need(conn, old["need_id"])
    target = entities.get_class(conn, target_class_id)
    if not target:
        raise ValueError(f"目标班级 {target_class_id} 不存在")
    if (target["skill_code"], target["level"], target["year"]) != (
        need["skill_code"], need["target_level"], need["year"]
    ):
        raise ValueError("目标班级的技能/等级/年度与需求不一致")
    if target["region"] not in need["allowed_regions"]:
        raise ValueError("目标班级地域不在需求允许范围内")
    provider = entities.get_provider(conn, target["provider_id"])
    if not provider["active"] or target["status"] != "open":
        raise ValueError("目标班级不可报名（机构停课或班级未开放）")
    from .allocator import class_remaining
    if class_remaining(conn, target) <= 0:
        raise ValueError("目标班级已满")
    q = quota_mod.get_quota(conn, target["year"], target["region"], target["skill_code"])
    if q is None:
        raise ValueError("目标班级所在地未配置年度配额")
    # 同一年度/地域/技能桶内转班，旧名额会先回收，计算可用量时计入这一个释放
    old_cls = entities.get_class(conn, old["class_id"])
    own_release = (
        1 if (old_cls["region"], old["skill_code"], old["year"])
        == (target["region"], target["skill_code"], target["year"]) else 0
    )
    if q["available"] + own_release <= 0:
        raise ValueError("目标班级所在地年度配额不足")

    # 旧班释放
    quota_mod.release(conn, old["year"], old_cls["region"], old["skill_code"], 1,
                      ref_id=old["id"], actor=actor, reason=f"转班至 {target_class_id}")
    conn.execute(
        "UPDATE enrollment SET status='transferred', reason=?, updated_at=? WHERE id=?",
        (f"转班至 {target_class_id}：{reason}", now_iso(), enr_id),
    )
    # 新建一条报名记录承接学员
    from .util import new_id
    new_enr_id = new_id("enr")
    conn.execute(
        """INSERT INTO enrollment
               (id, need_id, student_id, class_id, skill_code, year, status,
                run_id, transferred_from, reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (new_enr_id, old["need_id"], old["student_id"], target_class_id,
         old["skill_code"], old["year"], "registered", old["run_id"],
         enr_id, reason, now_iso(), now_iso()),
    )
    quota_mod.consume(conn, target["year"], target["region"], target["skill_code"], 1,
                      new_enr_id, actor=actor)
    audit.append(actor, "enrollment.transfer", "enrollment", new_enr_id,
                 {"from_enrollment": enr_id, "from_class": old["class_id"],
                  "to_class": target_class_id, "student_id": old["student_id"],
                  "reason": reason})
    return {"old_enrollment_id": enr_id, "new_enrollment_id": new_enr_id,
            "from_class": old["class_id"], "to_class": target_class_id}


def _requeue(conn, old_enr: dict, actor: str) -> None:
    """班级取消后把学员重新放入候补队列，等待其他班级有名额。"""
    from .util import new_id
    # 旧 enrollment 已 cancelled；建一条 waitlisted enrollment + waitlist_entry
    enr_id = new_id("enr")
    conn.execute(
        """INSERT INTO enrollment
               (id, need_id, student_id, class_id, skill_code, year, status,
                run_id, transferred_from, reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?, 'waitlisted', NULL, NULL, ?, ?, ?)""",
        (enr_id, old_enr["need_id"], old_enr["student_id"], None,
         old_enr["skill_code"], old_enr["year"], "课程取消后重新候补",
         now_iso(), now_iso()),
    )
    ent = conn.execute(
        "SELECT priority FROM enterprise e JOIN training_need n ON n.enterprise_id=e.id "
        "WHERE n.id=?",
        (old_enr["need_id"],),
    ).fetchone()
    conn.execute(
        """INSERT INTO waitlist_entry
               (id, enrollment_id, need_id, student_id, preferred_class_id,
                skill_code, year, priority, status, attempts, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,'pending',0,?,?)""",
        (new_id("wl"), enr_id, old_enr["need_id"], old_enr["student_id"], None,
         old_enr["skill_code"], old_enr["year"], ent["priority"] if ent else 0,
         now_iso(), now_iso()),
    )
