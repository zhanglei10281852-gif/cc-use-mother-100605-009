"""SQLite 连接与表结构。

所有状态都落库，保证服务重启后候补队列、配额台账、审计链可以完整恢复。
仅使用标准库，便于在离线环境中直接运行。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS provider (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        region TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS enterprise (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        region TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS student (
        id TEXT PRIMARY KEY,
        id_hash TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        home_region TEXT NOT NULL,
        enterprise_id TEXT REFERENCES enterprise(id),
        skills_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS class_session (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES provider(id),
        skill_code TEXT NOT NULL,
        level INTEGER NOT NULL,
        region TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        year INTEGER NOT NULL,
        starts_on TEXT,
        ends_on TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS training_need (
        id TEXT PRIMARY KEY,
        enterprise_id TEXT NOT NULL REFERENCES enterprise(id),
        skill_code TEXT NOT NULL,
        target_level INTEGER NOT NULL,
        allowed_regions_json TEXT NOT NULL,
        headcount INTEGER NOT NULL,
        year INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS need_candidate (
        need_id TEXT NOT NULL REFERENCES training_need(id),
        student_id TEXT NOT NULL REFERENCES student(id),
        seq INTEGER NOT NULL,
        PRIMARY KEY (need_id, student_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS enrollment (
        id TEXT PRIMARY KEY,
        need_id TEXT NOT NULL REFERENCES training_need(id),
        student_id TEXT NOT NULL REFERENCES student(id),
        class_id TEXT REFERENCES class_session(id),
        skill_code TEXT NOT NULL,
        year INTEGER NOT NULL,
        status TEXT NOT NULL,
        run_id TEXT,
        transferred_from TEXT,
        reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_enrollment_student
        ON enrollment(student_id, skill_code, year, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_enrollment_class ON enrollment(class_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS allocation_run (
        id TEXT PRIMARY KEY,
        actor TEXT,
        note TEXT,
        status TEXT NOT NULL,
        stats_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS allocation_decision (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES allocation_run(id),
        need_id TEXT NOT NULL,
        student_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        result TEXT NOT NULL,
        class_id TEXT,
        reason TEXT,
        trace_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quota (
        year INTEGER NOT NULL,
        region TEXT NOT NULL,
        skill_code TEXT NOT NULL,
        total_quota INTEGER NOT NULL,
        carry_in INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (year, region, skill_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_ledger (
        id TEXT PRIMARY KEY,
        year INTEGER,
        region TEXT,
        skill_code TEXT,
        delta_consumed INTEGER NOT NULL,
        tx_type TEXT NOT NULL,
        ref_id TEXT,
        actor TEXT,
        detail_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS waitlist_entry (
        id TEXT PRIMARY KEY,
        enrollment_id TEXT NOT NULL REFERENCES enrollment(id),
        need_id TEXT NOT NULL,
        student_id TEXT NOT NULL,
        preferred_class_id TEXT,
        skill_code TEXT NOT NULL,
        year INTEGER NOT NULL,
        priority INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_waitlist_status
        ON waitlist_entry(status, priority, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS import_batch (
        id TEXT PRIMARY KEY,
        actor TEXT,
        raw_students INTEGER NOT NULL,
        new_students INTEGER NOT NULL,
        duplicate_students INTEGER NOT NULL,
        raw_needs INTEGER NOT NULL,
        new_needs INTEGER NOT NULL,
        duplicate_needs INTEGER NOT NULL,
        summary_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS carryover (
        id TEXT PRIMARY KEY,
        from_year INTEGER NOT NULL,
        to_year INTEGER NOT NULL,
        region TEXT NOT NULL,
        skill_code TEXT NOT NULL,
        unused INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (from_year, to_year, region, skill_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL UNIQUE,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        payload_json TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    )
    """,
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
