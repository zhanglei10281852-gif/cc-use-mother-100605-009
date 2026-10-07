"""可解释分配引擎。

对每个 (需求, 候选学员) 顺序执行规则，每一步都记录规则名、输入与通过/拒绝原因，
最终轨迹写入 allocation_decision.trace_json，管理员可复盘某次分配为何这样安排。

规则集合：
  R1 需求有效        需求仍开放、未超过需求人数
  R2 无重复安排      同学员同技能同年度未被有效安排，且未达到目标等级
  R3 能力前置        学员当前等级低于目标等级，且差距在可培养范围内
  R4 课程匹配        班级技能/等级与需求一致
  R5 地域约束        班级地域在需求允许地域内
  R6 机构状态        承办机构在营、班级开放报名
  R7 班级容量        班级尚有余量
  R8 年度配额        该年度/地域/技能尚有配额
"""
from __future__ import annotations

from . import entities, quota as quota_mod
from .util import dumps, new_id, now_iso

# 学员当前等级与目标等级的最大可培养差距
MAX_SKILL_GAP = 2

# 占用班级名额与年度配额的报名状态
SEAT_HELD_STATUSES = ("registered", "attended", "completed")
# 已“有效安排”的状态，用于重复安排检查
PLACED_STATUSES = ("registered", "attended", "completed")


class Trace:
    def __init__(self):
        self.steps: list[dict] = []

    def add(self, rule: str, passed: bool, detail: str, **extra) -> dict:
        step = {"rule": rule, "passed": passed, "detail": detail}
        step.update(extra)
        self.steps.append(step)
        return step

    def failed(self) -> dict | None:
        return next((s for s in self.steps if not s["passed"]), None)


def class_remaining(conn, cls: dict) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM enrollment WHERE class_id=? AND status IN ({','.join('?'*len(SEAT_HELD_STATUSES))})",
        (cls["id"], *SEAT_HELD_STATUSES),
    ).fetchone()
    return cls["capacity"] - row["c"]


def existing_placement(conn, student_id: str, skill_code: str, year: int) -> dict | None:
    row = conn.execute(
        f"""SELECT e.* FROM enrollment e
            WHERE e.student_id=? AND e.skill_code=? AND e.year=?
              AND e.status IN ({','.join('?'*len(PLACED_STATUSES))})
            ORDER BY e.created_at DESC LIMIT 1""",
        (student_id, skill_code, year, *PLACED_STATUSES),
    ).fetchone()
    return dict(row) if row else None


def evaluate_candidate(conn, need: dict, student: dict, enterprise: dict) -> dict:
    """评估单个候选学员，返回 trace、候选班级和阻塞原因分类。"""
    trace = Trace()

    # R2 重复安排
    placed = existing_placement(conn, student["id"], need["skill_code"], need["year"])
    if placed:
        trace.add("R2_no_duplicate", False,
                  f"学员已被安排到班级 {placed['class_id']}（状态 {placed['status']}）",
                  existing_enrollment=placed["id"])
        return {"trace": trace, "eligible": False, "capacity_blocked": False}
    trace.add("R2_no_duplicate", True, "同学员同技能同年度无有效安排")

    # R3 能力前置
    current = int(student.get("skills", {}).get(need["skill_code"], 0))
    gap = need["target_level"] - current
    if current >= need["target_level"]:
        trace.add("R3_skill_prerequisite", False,
                  f"学员当前等级 {current} 已达到/超过目标等级 {need['target_level']}，无需培训",
                  current_level=current)
        return {"trace": trace, "eligible": False, "capacity_blocked": False}
    if gap > MAX_SKILL_GAP:
        trace.add("R3_skill_prerequisite", False,
                  f"当前等级 {current} 距目标 {need['target_level']} 差 {gap} 级，"
                  f"超过最大可培养跨度 {MAX_SKILL_GAP}",
                  current_level=current, gap=gap)
        return {"trace": trace, "eligible": False, "capacity_blocked": False}
    trace.add("R3_skill_prerequisite", True,
              f"当前等级 {current}，目标 {need['target_level']}，跨度 {gap} 在培养范围内",
              current_level=current, gap=gap)

    # R4/R5/R6/R7/R8：逐班评估
    classes = conn.execute(
        "SELECT * FROM class_session WHERE skill_code=? AND level=? AND year=?",
        (need["skill_code"], need["target_level"], need["year"]),
    ).fetchall()
    class_traces = []
    soft_blocked = False  # 仅因容量/配额不足而无法安排
    for crow in classes:
        cls = dict(crow)
        ct = Trace()
        ok = True

        if cls["region"] not in need["allowed_regions"]:
            ct.add("R5_region", False,
                   f"班级地域 {cls['region']} 不在允许地域 {need['allowed_regions']} 内")
            ok = False
        else:
            ct.add("R5_region", True, f"班级地域 {cls['region']} 满足约束")
        if not ok:
            class_traces.append({"class_id": cls["id"], "steps": ct.steps, "viable": False})
            continue

        provider = entities.get_provider(conn, cls["provider_id"])
        if not provider["active"] or cls["status"] != "open":
            ct.add("R6_provider_status", False,
                   f"机构在营={bool(provider['active'])}，班级状态={cls['status']}")
            class_traces.append({"class_id": cls["id"], "steps": ct.steps, "viable": False})
            continue
        ct.add("R6_provider_status", True,
               f"机构 {provider['name']} 在营，班级开放报名")

        remaining = class_remaining(conn, cls)
        if remaining <= 0:
            ct.add("R7_capacity", False, f"班级容量 {cls['capacity']} 已满", remaining=0)
            soft_blocked = True
            class_traces.append({"class_id": cls["id"], "steps": ct.steps, "viable": False})
            continue
        ct.add("R7_capacity", True, f"剩余名额 {remaining}", remaining=remaining)

        q = quota_mod.get_quota(conn, need["year"], cls["region"], need["skill_code"])
        if q is None:
            ct.add("R8_annual_quota", False,
                   f"{need['year']} 年 {cls['region']}/{need['skill_code']} 未配置年度配额")
            soft_blocked = True
            class_traces.append({"class_id": cls["id"], "steps": ct.steps, "viable": False})
            continue
        if q["available"] <= 0:
            ct.add("R8_annual_quota", False,
                   f"年度配额已用尽（总额 {q['total_quota'] + q['carry_in']}，已用 {q['used']}）",
                   available=0)
            soft_blocked = True
            class_traces.append({"class_id": cls["id"], "steps": ct.steps, "viable": False})
            continue
        ct.add("R8_annual_quota", True,
               f"年度配额余量 {q['available']}", available=q["available"])

        class_traces.append({"class_id": cls["id"], "steps": ct.steps, "viable": True,
                             "remaining": remaining})

    trace.add("R4_class_match", len(classes) > 0,
              f"年度内共有 {len(classes)} 个同技能同等级班级",
              class_count=len(classes))

    chosen = next((ct for ct in class_traces if ct["viable"]), None)
    if chosen:
        return {"trace": trace, "eligible": True, "capacity_blocked": False,
                "class_id": chosen["class_id"], "class_traces": class_traces}

    if class_traces and soft_blocked:
        return {"trace": trace, "eligible": True, "capacity_blocked": True,
                "class_id": None, "class_traces": class_traces}

    reasons = "；".join(
        f"{ct['class_id']}: " + next((s['detail'] for s in ct['steps'] if not s['passed']), "不匹配")
        for ct in class_traces
    ) or "年度内没有同技能同等级的班级"
    trace.add("R_match", False, f"没有可安排的班级。{reasons}")
    return {"trace": trace, "eligible": False, "capacity_blocked": False,
            "class_traces": class_traces}


def run_allocation(conn, audit, actor: str = "scheduler", note: str = "",
                   need_ids: list[str] | None = None) -> dict:
    """执行一轮分配。企业按优先级排序，候选学员按需求内顺序处理。"""
    run_id = new_id("run")
    if need_ids is None:
        need_rows = conn.execute(
            """SELECT n.id FROM training_need n JOIN enterprise e ON n.enterprise_id=e.id
               WHERE n.status='open'
               ORDER BY e.priority DESC, n.created_at ASC"""
        ).fetchall()
        need_ids = [r["id"] for r in need_rows]

    conn.execute(
        "INSERT INTO allocation_run (id, actor, note, status, stats_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, actor, note, "running", dumps({}), now_iso()),
    )
    stats = {"assigned": 0, "waitlisted": 0, "rejected": 0, "skipped": 0, "needs": 0}
    for nid in need_ids:
        need = entities.get_need(conn, nid)
        if not need or need["status"] != "open":
            continue
        stats["needs"] += 1
        enterprise = conn.execute(
            "SELECT * FROM enterprise WHERE id=?", (need["enterprise_id"],)
        ).fetchone()
        enterprise = dict(enterprise)
        remaining_headcount = need["headcount"]
        all_decisions = []

        for seq, sid in enumerate(need["candidate_ids"]):
            student = entities.get_student(conn, sid)
            trace = Trace()
            class_evaluation: list[dict] = []
            trace.add("R1_need_valid", remaining_headcount > 0,
                      f"需求人数 {need['headcount']}，剩余可分配 {max(remaining_headcount, 0)}")
            if remaining_headcount <= 0:
                result, class_id = "skipped", None
                reason = "需求名额已录满"
                stats["skipped"] += 1
            else:
                ev = evaluate_candidate(conn, need, student, enterprise)
                class_evaluation = ev.get("class_traces", [])
                for s in ev["trace"].steps:
                    trace.steps.append(s)
                class_traces = ev.get("class_traces", [])
                if ev["eligible"] and ev.get("class_id"):
                    class_id = ev["class_id"]
                    cls = entities.get_class(conn, class_id)
                    enr_id = _create_enrollment(
                        conn, need["id"], sid, class_id, need["skill_code"],
                        need["year"], "registered", run_id
                    )
                    quota_mod.consume(conn, need["year"], cls["region"],
                                      need["skill_code"], 1, enr_id, actor="scheduler")
                    result, reason = "assigned", f"分配至班级 {class_id}"
                    remaining_headcount -= 1
                    stats["assigned"] += 1
                elif ev["eligible"] and ev.get("capacity_blocked"):
                    enr_id = _create_enrollment(
                        conn, need["id"], sid, None, need["skill_code"],
                        need["year"], "waitlisted", run_id
                    )
                    conn.execute(
                        """INSERT INTO waitlist_entry
                               (id, enrollment_id, need_id, student_id, preferred_class_id,
                                skill_code, year, priority, status, attempts, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?, 'pending', 0, ?, ?)""",
                        (new_id("wl"), enr_id, need["id"], sid,
                         _preferred_class(class_traces), need["skill_code"], need["year"],
                         enterprise["priority"], now_iso(), now_iso()),
                    )
                    result, class_id, reason = "waitlisted", None, "匹配班级均满员或配额不足，已进入候补"
                    stats["waitlisted"] += 1
                else:
                    f = ev["trace"].failed()
                    result, class_id, reason = "rejected", None, f["detail"] if f else "不满足分配规则"
                    stats["rejected"] += 1

            dec_id = new_id("dec")
            conn.execute(
                """INSERT INTO allocation_decision
                       (id, run_id, need_id, student_id, seq, result, class_id, reason, trace_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (dec_id, run_id, nid, sid, seq, result, class_id, reason,
                 dumps({"steps": trace.steps, "class_evaluation": class_evaluation})),
            )
            all_decisions.append({"student_id": sid, "result": result,
                                  "class_id": class_id, "reason": reason})

        if remaining_headcount <= 0:
            conn.execute("UPDATE training_need SET status='allocated' WHERE id=?", (nid,))
        audit.append(actor, "allocation.run_need", "need", nid,
                     {"run_id": run_id, "remaining_headcount": remaining_headcount,
                      "decisions": all_decisions})

    conn.execute(
        "UPDATE allocation_run SET status='completed', stats_json=? WHERE id=?",
        (dumps(stats), run_id),
    )
    audit.append(actor, "allocation.run", "allocation_run", run_id,
                 {"note": note, "stats": stats})
    return {"run_id": run_id, "stats": stats}


def _preferred_class(class_traces: list[dict]) -> str | None:
    for ct in class_traces:
        failed_step = next((s for s in ct["steps"] if not s["passed"]), None)
        if failed_step and failed_step["rule"] in ("R7_capacity", "R8_annual_quota"):
            return ct["class_id"]
    return class_traces[0]["class_id"] if class_traces else None


def _create_enrollment(conn, need_id, student_id, class_id, skill_code, year,
                       status, run_id, transferred_from=None, reason=None) -> str:
    enr_id = new_id("enr")
    conn.execute(
        """INSERT INTO enrollment
               (id, need_id, student_id, class_id, skill_code, year, status,
                run_id, transferred_from, reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (enr_id, need_id, student_id, class_id, skill_code, year, status,
         run_id, transferred_from, reason, now_iso(), now_iso()),
    )
    return enr_id


def get_run(conn, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM allocation_run WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["stats"] = __import__("json").loads(d.pop("stats_json"))
    d["decisions"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM allocation_decision WHERE run_id=? ORDER BY need_id, seq", (run_id,)
        ).fetchall()
    ]
    return d
