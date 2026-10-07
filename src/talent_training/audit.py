"""哈希链审计日志。

每条审计记录包含前一条记录的哈希，形成只能追加、不可中途篡改的链条。
verify_chain() 可在任何时刻重算整条链，发现删除或改写。
学员转班、机构停课、名额释放、结转、重复导入等所有状态变化都在此留痕。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading

from .util import dumps, new_id, now_iso

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    def append(
        self,
        actor: str,
        action: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload: object | None = None,
    ) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["hash"] if row else GENESIS
            seq_row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM audit_log").fetchone()
            seq = seq_row["s"]
            ts = now_iso()
            body = dumps(
                {
                    "seq": seq,
                    "ts": ts,
                    "actor": actor,
                    "action": action,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "payload": payload if payload is not None else {},
                    "prev_hash": prev_hash,
                }
            )
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            record_id = new_id("aud")
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (id, seq, ts, actor, action, entity_type, entity_id,
                     payload_json, prev_hash, hash)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    seq,
                    ts,
                    actor,
                    action,
                    entity_type,
                    entity_id,
                    dumps(payload if payload is not None else {}),
                    prev_hash,
                    digest,
                ),
            )
            return {"id": record_id, "seq": seq, "ts": ts, "hash": digest}

    def list(self, limit: int = 100, entity_type: str | None = None, entity_id: str | None = None):
        sql = "SELECT * FROM audit_log"
        clauses, params = [], []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def verify_chain(self) -> dict:
        """重算整条哈希链，返回校验结果与断裂位置。"""
        rows = self._conn.execute("SELECT * FROM audit_log ORDER BY seq ASC").fetchall()
        prev_hash = GENESIS
        expected_seq = 1
        for row in rows:
            if row["seq"] != expected_seq:
                return {"ok": False, "broken_at_seq": row["seq"], "reason": "序号不连续"}
            if row["prev_hash"] != prev_hash:
                return {"ok": False, "broken_at_seq": row["seq"], "reason": "前序哈希不匹配"}
            body = dumps(
                {
                    "seq": row["seq"],
                    "ts": row["ts"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "payload": loads_safe(row["payload_json"]),
                    "prev_hash": row["prev_hash"],
                }
            )
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if digest != row["hash"]:
                return {"ok": False, "broken_at_seq": row["seq"], "reason": "内容哈希不匹配"}
            prev_hash = digest
            expected_seq += 1
        return {"ok": True, "entries": len(rows), "head_hash": prev_hash}


def loads_safe(text: str):
    import json

    return json.loads(text)
