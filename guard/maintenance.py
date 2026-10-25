"""惰性的后台清理：在每次 fetch / 审批 / 查询前顺手跑一遍，不需要单独的定时任务。

- 过期的待审批单 → 审批单 expired，支付请求 PENDING_APPROVAL → EXPIRED
- 过期的预算预留：支付请求还停在 RESERVED / SIGNING（没有发出任何交易）→ EXPIRED 并释放预留
  已经 SUBMITTED / UNKNOWN_SETTLEMENT 的绝不因为超时释放，只能由对账决定
- UNKNOWN_SETTLEMENT 的请求 → 按交易哈希查链对账
"""

from guard import approvals, audit, budget, payments
from guard import state_machine as sm
from guard.settlement import reconcile_all_unknown


def sweep() -> None:
    from guard import escrow
    expire_approvals()
    expire_reservations()
    reconcile_all_unknown()
    escrow.sweep()


def expire_approvals() -> None:
    for a in approvals.expired_pending():
        reason = "Approval expired: not approved within approval_ttl_seconds"
        approvals.mark_expired(a["id"], reason)
        audit.mark_expired(a["decision_id"], reason)
        try:
            sm.advance(a["payment_request_id"], sm.EXPIRED, error=reason)
        except sm.IllegalTransition:
            pass   # 已被并发处理


def expire_reservations() -> None:
    for r in budget.expired_reservations():
        row = payments.get(r["payment_request_id"])
        if row["state"] in (sm.RESERVED, sm.SIGNING):
            try:
                sm.advance(row["id"], sm.EXPIRED, error="Budget reservation expired; payment not sent")
            except sm.IllegalTransition:
                continue
            budget.release(row["id"])
        elif row["state"] in sm.TERMINAL - sm.PAID_STATES:
            budget.release(row["id"])   # 兜底：终态但预留没释放
