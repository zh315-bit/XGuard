"""审计日志：每次判定写一行 decisions，付款结果回写同一行。

同时为策略提供历史统计（今日已付、近期请求次数）。
"""

import time
from datetime import datetime, timezone
from typing import Optional

from guard.db import connect
from guard.policy import Decision
from guard.quote import Quote


def log_decision(url: str, purpose: str, quote: Optional[Quote], decision: Decision, status: str) -> int:
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO decisions (ts, created_at, url, purpose, network, asset, amount, amount_xrp,
                                      pay_to, invoice_id, verdict, rule_id, reason, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, _iso(now), url, purpose,
             quote.network if quote else None,
             quote.asset if quote else None,
             quote.amount if quote else None,
             str(quote.amount_xrp) if quote and quote.amount_xrp is not None else None,
             quote.pay_to if quote else None,
             quote.invoice_id if quote else None,
             decision.verdict, decision.rule_id, decision.reason, status),
        )
        return cur.lastrowid


def mark_paid(decision_id: int, tx_hash: str) -> None:
    _update(decision_id, status="paid", tx_hash=tx_hash)


def mark_failed(decision_id: int, error: str, tx_hash: Optional[str] = None) -> None:
    _update(decision_id, status="pay_failed", error=error, tx_hash=tx_hash)


def mark_aborted(decision_id: int, error: str) -> None:
    """判了 AUTO 但付款前被守卫自己中止（例如报价调包），确定没有付款。"""
    _update(decision_id, status="aborted", error=error)


def mark_approved_paid(decision_id: int, tx_hash: str) -> None:
    """HOLD 经人工批准、由金库付款。和 AUTO 的 paid 区分开：不占用热钱包的日预算（R5）。"""
    _update(decision_id, status="approved_paid", tx_hash=tx_hash)


def mark_rejected(decision_id: int, reason: str) -> None:
    _update(decision_id, status="rejected", error=reason)


def list_decisions(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_refunded(decision_id: int, cancel_tx: str) -> None:
    """escrow 验货不通过，钱已经从 escrow 退回金库。"""
    _update(decision_id, status="refunded", tx_hash=cancel_tx)


def update_decision(decision_id: int, decision: Decision, status: str) -> None:
    """预算原子预留失败时，判定从 AUTO 改为 HOLD(R5) / DENY(R7)。"""
    _update(decision_id, verdict=decision.verdict, rule_id=decision.rule_id, reason=decision.reason, status=status)


def mark_expired(decision_id: int, reason: str) -> None:
    _update(decision_id, status="expired", error=reason)


def set_risk(decision_id: int, risk_json: str) -> None:
    _update(decision_id, risk_json=risk_json)


def get_decision(decision_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return dict(row) if row else None


def _update(decision_id: int, **fields: Optional[str]) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE decisions SET {cols} WHERE id = ?", (*fields.values(), decision_id))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
