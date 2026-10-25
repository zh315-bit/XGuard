"""payment_requests 表的存取。(agent_id, request_id) 唯一：同一个请求只会创建一行、只会付一次。"""

import json
import sqlite3
import time
from typing import Any, Optional

from guard.db import connect
from guard.state_machine import RECEIVED


def create_or_get(agent_id: str, request_id: str, url: str, purpose: str) -> tuple[dict, bool]:
    """返回 (行, 是否新建)。唯一索引冲突说明这个 request_id 已经来过，返回已有的那一行。"""
    now = time.time()
    try:
        with connect() as conn:
            cur = conn.execute(
                """INSERT INTO payment_requests (agent_id, request_id, created_at, updated_at, url, purpose, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, request_id, now, now, url, purpose, RECEIVED))
            payment_id = cur.lastrowid
        return get(payment_id), True
    except sqlite3.IntegrityError:
        return get_by_request(agent_id, request_id), False


def get(payment_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (payment_id,)).fetchone()
    return dict(row) if row else None


def get_by_request(agent_id: str, request_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM payment_requests WHERE agent_id = ? AND request_id = ?",
                           (agent_id, request_id)).fetchone()
    return dict(row) if row else None


def list_in_states(states: tuple[str, ...]) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM payment_requests WHERE state IN ({','.join('?' * len(states))})",
                            states).fetchall()
    return [dict(r) for r in rows]


def submitted_to_since(pay_to: str, since: float) -> int:
    """R6 用：窗口期内真正发出过签名交易的请求数（被拒、待审批的都不算）。"""
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM payment_requests WHERE pay_to = ? AND submitted_at >= ?",
                           (pay_to, since)).fetchone()
    return row["n"]


def set_error(payment_id: int, error: str) -> None:
    """只记录错误信息，不改变状态（状态只能经 state_machine.advance 修改）。"""
    with connect() as conn:
        conn.execute("UPDATE payment_requests SET error = ?, updated_at = ? WHERE id = ?", (error, time.time(), payment_id))


def set_risk(payment_id: int, risk_json: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE payment_requests SET risk_json = ? WHERE id = ?", (risk_json, payment_id))


def result(row: dict) -> Any:
    return json.loads(row["result_json"]) if row.get("result_json") else None
