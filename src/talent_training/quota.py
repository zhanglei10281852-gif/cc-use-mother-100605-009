"""年度名额配额：额度设置、消耗/释放台账与跨年度结转。

quota 表保存某年/地域/技能的总额度（含上年结转 carry_in）；
quota_ledger 是只追加台账，每一次消耗、释放、结转、调整都有流水，
可用余量 = total_quota + carry_in - SUM(已确认消耗流水)。
"""
from __future__ import annotations

from .util import dumps, new_id, now_iso

LEDGER_CONSUME = "consume"
LEDGER_RELEASE = "release"
LEDGER_ADJUST = "adjust"
LEDGER_CARRY_IN = "carry_in"


def set_quota(conn, audit, year: int, region: str, skill_code: str,
              total: int, actor: str = "admin") -> dict:
    if total < 0:
        raise ValueError("配额不能为负数")
    now = now_iso()
    conn.execute(
        """INSERT INTO quota (year, region, skill_code, total_quota, carry_in, updated_at)
           VALUES (?,?,?,?,0,?)
           ON CONFLICT(year, region, skill_code)
           DO UPDATE SET total_quota=excluded.total_quota, updated_at=excluded.updated_at""",
        (year, region, skill_code, total, now),
    )
    _ledger(conn, year, region, skill_code, 0, LEDGER_ADJUST,
            ref_id=None, actor=actor, detail={"set_total": total})
    audit.append(actor, "quota.set", "quota", f"{year}:{region}:{skill_code}",
                 {"total_quota": total})
    return get_quota(conn, year, region, skill_code)


def _ledger(conn, year, region, skill_code, amount: int, tx_type: str,
            ref_id=None, actor="system", detail=None) -> str:
    lid = new_id("led")
    conn.execute(
        """INSERT INTO quota_ledger
               (id, year, region, skill_code, delta_consumed, tx_type,
                ref_id, actor, detail_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (lid, year, region, skill_code, amount, tx_type, ref_id, actor,
         dumps(detail or {}), now_iso()),
    )
    return lid


def consume(conn, year: int, region: str, skill_code: str, amount: int,
            ref_id: str, actor: str = "system") -> None:
    _ledger(conn, year, region, skill_code, amount, LEDGER_CONSUME,
            ref_id=ref_id, actor=actor)


def release(conn, year: int, region: str, skill_code: str, amount: int,
            ref_id: str, actor: str = "system", reason: str | None = None) -> None:
    """释放名额（课程取消、候补转退出等）。负数消耗流水即回收。"""
    _ledger(conn, year, region, skill_code, -amount, LEDGER_RELEASE,
            ref_id=ref_id, actor=actor, detail={"reason": reason})


def used(conn, year: int, region: str, skill_code: str) -> int:
    """已消耗名额 = 消耗流水 + 释放流水（负值）；调整/结转流水不参与。"""
    row = conn.execute(
        """SELECT COALESCE(SUM(delta_consumed), 0) AS used
           FROM quota_ledger
           WHERE year=? AND region=? AND skill_code=?
             AND tx_type IN ('consume', 'release')""",
        (year, region, skill_code),
    ).fetchone()
    return int(row["used"])


def get_quota(conn, year: int, region: str, skill_code: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM quota WHERE year=? AND region=? AND skill_code=?",
        (year, region, skill_code),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["used"] = used(conn, year, region, skill_code)
    d["available"] = d["total_quota"] + d["carry_in"] - d["used"]
    return d


def list_quota(conn, year: int | None = None) -> list[dict]:
    rows = (
        conn.execute("SELECT year, region, skill_code FROM quota ORDER BY year, region, skill_code").fetchall()
        if year is None
        else conn.execute(
            "SELECT year, region, skill_code FROM quota WHERE year=? ORDER BY region, skill_code",
            (year,),
        ).fetchall()
    )
    return [get_quota(conn, r["year"], r["region"], r["skill_code"]) for r in rows]


def list_ledger(conn, year: int | None = None, limit: int = 200) -> list[dict]:
    if year is None:
        rows = conn.execute(
            "SELECT * FROM quota_ledger ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM quota_ledger WHERE year=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (year, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def carry_over(conn, audit, from_year: int, to_year: int, actor: str = "admin") -> dict:
    """把某年度未使用的名额结转到下一年度。

    每个 (地域, 技能) 维度只允许结转一次（carryover 表唯一约束），
    结转金额写入下年 carry_in 并产生 carry_in 流水，历史台账完整保留。
    """
    if to_year <= from_year:
        raise ValueError("结转目标年度必须晚于来源年度")
    existing = conn.execute(
        "SELECT 1 FROM carryover WHERE from_year=? AND to_year=?",
        (from_year, to_year),
    ).fetchone()
    if existing:
        raise ValueError(f"{from_year} -> {to_year} 已执行过结转，不能重复结转")

    rows = conn.execute(
        "SELECT region, skill_code, total_quota, carry_in FROM quota WHERE year=?",
        (from_year,),
    ).fetchall()
    result_items = []
    for row in rows:
        region, skill = row["region"], row["skill_code"]
        u = used(conn, from_year, region, skill)
        unused = row["total_quota"] + row["carry_in"] - u
        if unused <= 0:
            result_items.append({"region": region, "skill_code": skill, "unused": 0, "carried": 0})
            continue
        conn.execute(
            """INSERT INTO quota (year, region, skill_code, total_quota, carry_in, updated_at)
               VALUES (?,?,?,0,?,?)
               ON CONFLICT(year, region, skill_code)
               DO UPDATE SET carry_in = quota.carry_in + excluded.carry_in,
                             updated_at = excluded.updated_at""",
            (to_year, region, skill, unused, now_iso()),
        )
        conn.execute(
            """INSERT INTO carryover (id, from_year, to_year, region, skill_code, unused, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (new_id("carry"), from_year, to_year, region, skill, unused, now_iso()),
        )
        _ledger(conn, to_year, region, skill, 0, LEDGER_CARRY_IN,
                ref_id=None, actor=actor,
                detail={"from_year": from_year, "amount": unused})
        result_items.append({"region": region, "skill_code": skill,
                             "unused": unused, "carried": unused})

    audit.append(actor, "quota.carry_over", "carryover", f"{from_year}:{to_year}",
                 {"items": result_items})
    return {"from_year": from_year, "to_year": to_year, "items": result_items}
