"""approvals 表的存取。HOLD 的请求进这里等人工审批；审批流程见 approval_service.py。

状态：pending →（approving）→ approved / rejected / expired。批准是一次性的：
approving 是原子锁（try_lock），同一张单只有一个调用者能拿到，之后再调用 approve 都不会再付款。
审批单绑定报价条款哈希（quote_hash，含验收条件）和过期时间（expires_at = 创建时间 + approval_ttl_seconds），
并保存完整报价快照（snapshot_json）：验货只按这份快照里的描述和 dataSchema 判断。
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from guard.db import connect
from guard.policy import Decision
from guard.quote import Quote


def enqueue(quote: Quote, purpose: str, decision: Decision, decision_id: int, payment_id: int,
            ttl_seconds: int, risk: Optional[dict] = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO approvals (created_at, decision_id, url, purpose, quote_json, rule_id, reason, status,
                                      quote_hash, expires_at, payment_request_id, risk_json, snapshot_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (_now(), decision_id, quote.url, purpose, json.dumps(quote.requirements.to_dict()),
             decision.rule_id, decision.reason, quote.terms_hash(), time.time() + ttl_seconds, payment_id,
             json.dumps(risk, ensure_ascii=False) if risk else None,
             json.dumps(quote.snapshot(), ensure_ascii=False)),
        )
        return cur.lastrowid


def expired_pending(now: Optional[float] = None) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM approvals WHERE status = 'pending' AND expires_at < ?",
                            (now or time.time(),)).fetchall()
    return [dict(r) for r in rows]


def acceptance(approval: dict) -> dict[str, Any]:
    """审批单快照里的验收条件。没有快照的旧审批单返回空条件（哈希比对会让它们走 R8）。"""
    snapshot = json.loads(approval["snapshot_json"]) if approval.get("snapshot_json") else {}
    return snapshot.get("acceptance") or {}


def get(approval_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row) if row else None


def list_by_status(status: Optional[str] = "pending") -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute("SELECT * FROM approvals WHERE status = ? ORDER BY id", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM approvals ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def try_lock(approval_id: int) -> bool:
    """pending → approving，原子操作；返回 False 表示已被处理或正在处理。"""
    with connect() as conn:
        cur = conn.execute("UPDATE approvals SET status = 'approving' WHERE id = ? AND status = 'pending'",
                           (approval_id,))
        return cur.rowcount == 1


def mark_approved(approval_id: int, tx_hash: Optional[str], result: Any, error: Optional[str] = None) -> None:
    """记录“已批准”这个决定本身；付款结果（成功 / 未知 / 失败）以 payment_requests 的状态为准。"""
    _update(approval_id, status="approved", decided_at=_now(), tx_hash=tx_hash,
            result_json=json.dumps(result, ensure_ascii=False) if result is not None else None, last_error=error)


def mark_rejected(approval_id: int, reason: str) -> None:
    _update(approval_id, status="rejected", decided_at=_now(), reject_reason=reason)


def mark_expired(approval_id: int, reason: str) -> None:
    _update(approval_id, status="expired", decided_at=_now(), reject_reason=reason)


def _update(approval_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE approvals SET {cols} WHERE id = ?", (*fields.values(), approval_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
