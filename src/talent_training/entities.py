"""领域实体注册：机构、企业、学员、班级、培训需求，以及批量导入去重。"""
from __future__ import annotations

import hashlib
import sqlite3

from .util import dumps, loads, new_id, now_iso

LEVEL_MIN, LEVEL_MAX = 1, 5

CLASS_STATUSES = {"open", "suspended", "closed", "completed"}
NEED_STATUSES = {"open", "allocated", "closed"}


def _require(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field}不能为空")
    return value


def id_hash(external_id: str) -> str:
    return hashlib.sha256(external_id.strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 机构 / 企业

def create_provider(conn, audit, data: dict, actor: str = "admin") -> dict:
    pid = data.get("id") or new_id("prov")
    name = _require(data.get("name", ""), "机构名称")
    region = _require(data.get("region", ""), "机构地域")
    conn.execute(
        "INSERT INTO provider (id, name, region, active, created_at) VALUES (?,?,?,?,?)",
        (pid, name, region, 1, now_iso()),
    )
    audit.append(actor, "provider.create", "provider", pid, {"name": name, "region": region})
    return get_provider(conn, pid)


def get_provider(conn, pid: str) -> dict | None:
    row = conn.execute("SELECT * FROM provider WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def list_providers(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM provider ORDER BY id").fetchall()]


def create_enterprise(conn, audit, data: dict, actor: str = "admin") -> dict:
    eid = data.get("id") or new_id("ent")
    name = _require(data.get("name", ""), "企业名称")
    region = _require(data.get("region", ""), "企业地域")
    priority = int(data.get("priority", 0))
    conn.execute(
        "INSERT INTO enterprise (id, name, region, priority, created_at) VALUES (?,?,?,?,?)",
        (eid, name, region, priority, now_iso()),
    )
    audit.append(actor, "enterprise.create", "enterprise", eid,
                 {"name": name, "region": region, "priority": priority})
    return dict(conn.execute("SELECT * FROM enterprise WHERE id=?", (eid,)).fetchone())


def list_enterprises(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM enterprise ORDER BY id").fetchall()]


# ---------------------------------------------------------------- 学员

def _normalize_skills(skills: dict) -> dict:
    out: dict[str, int] = {}
    for code, level in (skills or {}).items():
        level = int(level)
        if not 0 <= level <= LEVEL_MAX:
            raise ValueError(f"技能 {code} 等级需在 0-{LEVEL_MAX} 之间（0 表示未掌握）")
        out[str(code).strip()] = level
    return out


def create_student(conn, audit, data: dict, actor: str = "admin") -> dict:
    sid = _require(data.get("id", ""), "学员证件号")
    name = _require(data.get("name", ""), "学员姓名")
    region = _require(data.get("home_region", ""), "学员户籍地")
    skills = _normalize_skills(data.get("skills"))
    enterprise_id = data.get("enterprise_id")
    if enterprise_id and not conn.execute(
        "SELECT 1 FROM enterprise WHERE id=?", (enterprise_id,)
    ).fetchone():
        raise ValueError(f"企业 {enterprise_id} 不存在")
    conn.execute(
        """INSERT INTO student (id, id_hash, name, home_region, enterprise_id, skills_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (sid, id_hash(sid), name, region, enterprise_id, dumps(skills), now_iso()),
    )
    audit.append(actor, "student.create", "student", sid,
                 {"name": name, "home_region": region, "skills": skills})
    return get_student(conn, sid)


def get_student(conn, sid: str) -> dict | None:
    row = conn.execute("SELECT * FROM student WHERE id=?", (sid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["skills"] = loads(d.pop("skills_json"), {})
    return d


def list_students(conn) -> list[dict]:
    return [get_student(conn, r["id"]) for r in conn.execute("SELECT id FROM student").fetchall()]


# ---------------------------------------------------------------- 班级

def create_class(conn, audit, data: dict, actor: str = "admin") -> dict:
    cid = data.get("id") or new_id("cls")
    provider_id = _require(data.get("provider_id", ""), "承办机构")
    skill_code = _require(data.get("skill_code", ""), "技能编码")
    level = int(data.get("level", 0))
    if not LEVEL_MIN <= level <= LEVEL_MAX:
        raise ValueError(f"课程等级需在 {LEVEL_MIN}-{LEVEL_MAX} 之间")
    region = _require(data.get("region", ""), "开班地域")
    capacity = int(data.get("capacity", 0))
    if capacity <= 0:
        raise ValueError("班级容量必须为正整数")
    year = int(data.get("year", 0))
    if year <= 0:
        raise ValueError("年度不合法")
    provider = get_provider(conn, provider_id)
    if not provider:
        raise ValueError(f"机构 {provider_id} 不存在")
    if not provider["active"]:
        raise ValueError(f"机构 {provider_id} 已停课，不能新开班级")
    conn.execute(
        """INSERT INTO class_session
               (id, provider_id, skill_code, level, region, capacity, status, year,
                starts_on, ends_on, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, provider_id, skill_code, level, region, capacity, "open", year,
         data.get("starts_on"), data.get("ends_on"), now_iso()),
    )
    audit.append(actor, "class.create", "class", cid,
                 {"provider_id": provider_id, "skill_code": skill_code, "level": level,
                  "region": region, "capacity": capacity, "year": year})
    return get_class(conn, cid)


def get_class(conn, cid: str) -> dict | None:
    row = conn.execute("SELECT * FROM class_session WHERE id=?", (cid,)).fetchone()
    return dict(row) if row else None


def list_classes(conn, year: int | None = None) -> list[dict]:
    if year is None:
        rows = conn.execute("SELECT * FROM class_session ORDER BY year, id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM class_session WHERE year=? ORDER BY id", (year,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 培训需求

def create_need(conn, audit, data: dict, actor: str = "admin", idempotency_key: str | None = None) -> dict:
    nid = data.get("id") or new_id("need")
    enterprise_id = _require(data.get("enterprise_id", ""), "提报企业")
    skill_code = _require(data.get("skill_code", ""), "技能编码")
    target_level = int(data.get("target_level", 0))
    if not LEVEL_MIN <= target_level <= LEVEL_MAX:
        raise ValueError(f"目标等级需在 {LEVEL_MIN}-{LEVEL_MAX} 之间")
    regions = data.get("allowed_regions") or []
    if isinstance(regions, str):
        regions = [regions]
    regions = sorted({r.strip() for r in regions if r.strip()})
    if not regions:
        raise ValueError("至少需要一个允许地域")
    headcount = int(data.get("headcount", 0))
    if headcount <= 0:
        raise ValueError("需求人数必须为正整数")
    year = int(data.get("year", 0))
    candidates = data.get("candidate_ids") or []
    if not candidates:
        raise ValueError("至少需要一名候选学员")
    enterprise = conn.execute(
        "SELECT * FROM enterprise WHERE id=?", (enterprise_id,)
    ).fetchone()
    if not enterprise:
        raise ValueError(f"企业 {enterprise_id} 不存在")
    key = idempotency_key or hashlib.sha256(
        dumps([enterprise_id, skill_code, target_level, year, sorted(regions),
               headcount, sorted(candidates)]).encode("utf-8")
    ).hexdigest()
    existing = conn.execute(
        "SELECT id FROM training_need WHERE idempotency_key=?", (key,)
    ).fetchone()
    if existing:
        return {"id": existing["id"], "duplicate": True}

    conn.execute(
        """INSERT INTO training_need
               (id, enterprise_id, skill_code, target_level, allowed_regions_json,
                headcount, year, idempotency_key, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (nid, enterprise_id, skill_code, target_level, dumps(regions), headcount, year,
         key, "open", now_iso()),
    )
    for seq, sid in enumerate(candidates):
        if not conn.execute("SELECT 1 FROM student WHERE id=?", (sid,)).fetchone():
            raise ValueError(f"候选学员 {sid} 不存在")
        conn.execute(
            "INSERT INTO need_candidate (need_id, student_id, seq) VALUES (?,?,?)",
            (nid, sid, seq),
        )
    audit.append(actor, "need.create", "need", nid,
                 {"enterprise_id": enterprise_id, "skill_code": skill_code,
                  "target_level": target_level, "allowed_regions": regions,
                  "headcount": headcount, "year": year,
                  "candidates": candidates, "idempotency_key": key[:16]})
    return {"id": nid, "duplicate": False}


def get_need(conn, nid: str) -> dict | None:
    row = conn.execute("SELECT * FROM training_need WHERE id=?", (nid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["allowed_regions"] = loads(d.pop("allowed_regions_json"), [])
    d["candidate_ids"] = [
        r["student_id"]
        for r in conn.execute(
            "SELECT student_id FROM need_candidate WHERE need_id=? ORDER BY seq", (nid,)
        ).fetchall()
    ]
    return d


def list_needs(conn, status: str | None = None) -> list[dict]:
    rows = (
        conn.execute("SELECT id FROM training_need ORDER BY created_at").fetchall()
        if status is None
        else conn.execute(
            "SELECT id FROM training_need WHERE status=? ORDER BY created_at", (status,)
        ).fetchall()
    )
    return [get_need(conn, r["id"]) for r in rows]


# ---------------------------------------------------------------- 批量导入

def import_batch(conn, audit, payload: dict, actor: str = "admin") -> dict:
    """一次性导入学员与需求；重复数据自动识别并跳过，全过程留痕。

    学员按证件号去重；需求按业务字段指纹去重。
    整批在一个事务内，任意一条非法数据则整批回滚。
    """
    raw_students = payload.get("students") or []
    raw_needs = payload.get("needs") or []
    summary = {"students": [], "needs": []}
    new_students = dup_students = new_needs = dup_needs = 0
    try:
        for s in raw_students:
            sid = _require(s.get("id", ""), "学员证件号")
            if conn.execute("SELECT 1 FROM student WHERE id=?", (sid,)).fetchone():
                dup_students += 1
                summary["students"].append({"id": sid, "result": "duplicate"})
                continue
            create_student(conn, _NullAudit(), s, actor=actor)
            new_students += 1
            summary["students"].append({"id": sid, "result": "created"})

        for n in raw_needs:
            result = create_need(conn, _NullAudit(), n, actor=actor)
            if result.get("duplicate"):
                dup_needs += 1
                summary["needs"].append({"id": result["id"], "result": "duplicate"})
            else:
                new_needs += 1
                summary["needs"].append({"id": result["id"], "result": "created"})
    except Exception:
        conn.rollback()
        raise

    batch_id = new_id("imp")
    conn.execute(
        """INSERT INTO import_batch
               (id, actor, raw_students, new_students, duplicate_students,
                raw_needs, new_needs, duplicate_needs, summary_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (batch_id, actor, len(raw_students), new_students, dup_students,
         len(raw_needs), new_needs, dup_needs, dumps(summary), now_iso()),
    )
    audit.append(actor, "import.batch", "import_batch", batch_id,
                 {"raw_students": len(raw_students), "new_students": new_students,
                  "duplicate_students": dup_students, "raw_needs": len(raw_needs),
                  "new_needs": new_needs, "duplicate_needs": dup_needs})
    return {
        "batch_id": batch_id,
        "raw_students": len(raw_students),
        "new_students": new_students,
        "duplicate_students": dup_students,
        "raw_needs": len(raw_needs),
        "new_needs": new_needs,
        "duplicate_needs": dup_needs,
        "detail": summary,
    }


class _NullAudit:
    """批量导入内部逐条创建时不单独记审计，由整批审计统一覆盖。"""

    def append(self, *args, **kwargs):
        return None
