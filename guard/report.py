"""今日花费报告（UTC 日，与预算同一口径），给 Mission 页顶部的大数字卡片用。只读，不含补款（金库 → 热钱包是内部调拨）。"""

from datetime import datetime, timezone
from typing import Any

from guard import state_machine as sm
from guard.budget import _today
from guard.db import connect
from guard.policy import fmt_xrp


def today() -> dict[str, Any]:
    day = _today()
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    with connect() as conn:
        spent = conn.execute(
            """SELECT COALESCE(SUM(b.amount_drops), 0) FROM budget_reservations b
               JOIN payment_requests p ON p.id = b.payment_request_id
               WHERE b.day = ? AND b.status = 'spent' AND p.method != 'topup'""", (day,)).fetchone()[0]
        counts = conn.execute(
            """SELECT
                 SUM(p.state = ?)                                AS purchased,
                 SUM(p.state = ? AND p.verdict = 'AUTO')         AS auto_paid,
                 SUM(a.status = 'approved')                      AS approved,
                 SUM(p.state = ?)                                AS denied,
                 SUM(p.state = ?)                                AS rejected
               FROM payment_requests p LEFT JOIN approvals a ON a.id = p.approval_id
               WHERE p.created_at >= ? AND p.method != 'topup'""",
            (sm.RESOURCE_DELIVERED, sm.RESOURCE_DELIVERED, sm.DENIED, sm.REJECTED, start)).fetchone()
        refunded = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_drops), 0) FROM escrows WHERE status = 'refunded' AND created_at >= ?",
            (start,)).fetchone()
    denied, rejected = counts["denied"] or 0, counts["rejected"] or 0
    return {
        "day": day,
        "spent_xrp": fmt_xrp(spent),
        "purchased": counts["purchased"] or 0,
        "auto_paid": counts["auto_paid"] or 0,
        "approved": counts["approved"] or 0,
        "blocked": denied + rejected,                 # 策略拒绝 + 审批人拒绝，都没有付款
        "blocked_by_policy": denied,
        "rejected_by_human": rejected,
        "escrow_refunded_xrp": fmt_xrp(refunded[1]),
        "escrow_refunded_count": refunded[0],
    }
