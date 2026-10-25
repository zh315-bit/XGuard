"""agent 任务日志：agent 进程通过 HTTP 上报每一步（工具调用、结果、文字），控制台的 Mission 页面展示。

只是展示用的记录，不参与任何付款判定。
"""

import json
import time
from typing import Any, Optional

from guard.db import connect


def create(task: str, source: str = "cli", pid: Optional[int] = None) -> int:
    with connect() as conn:
        return conn.execute("INSERT INTO missions (created_at, task, status, source, pid) VALUES (?, ?, 'running', ?, ?)",
                            (time.time(), task, source, pid)).lastrowid


def get(mission_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
    return dict(row) if row else None


def set_pid(mission_id: int, pid: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE missions SET pid = ? WHERE id = ?", (pid, mission_id))


def running() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM missions WHERE status = 'running' ORDER BY id")]


def add_event(mission_id: int, kind: str, name: Optional[str], payload: Any) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO mission_events (mission_id, ts, kind, name, payload) VALUES (?, ?, ?, ?, ?)",
                     (mission_id, time.time(), kind, name, json.dumps(payload, ensure_ascii=False)))


def finish(mission_id: int, status: str, report: Optional[str]) -> bool:
    """只结束还在运行的任务（已取消的任务，agent 进程晚到的 finish 不会把它改回 done）。"""
    with connect() as conn:
        cur = conn.execute("UPDATE missions SET status = ?, report = ?, finished_at = ? WHERE id = ? AND status = 'running'",
                           (status, report, time.time(), mission_id))
        return cur.rowcount == 1


def latest() -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM missions ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        events = conn.execute("SELECT * FROM mission_events WHERE mission_id = ? ORDER BY id", (row["id"],)).fetchall()
    return {**dict(row), "events": [{**dict(e), "payload": json.loads(e["payload"])} for e in events]}
