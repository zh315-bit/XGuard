"""把支付请求的状态翻译成给 agent / API 调用方看的结果（只放白名单字段，不含任何密钥）。"""

from typing import Any

from guard import state_machine as sm
from guard.config import ROOT
from guard.policy import fmt_xrp
from guard.x402_gate import GateResult

APPROVE_HINT = f"cd {ROOT} && .venv/bin/python scripts/approve.py approve {{approval_id}}"

_NEXT_STEP = {
    "paid": None,
    "free": None,
    "pending_approval": "Call wait_for_approval(approval_id) to wait for human approval; do not request again",
    "denied": "Do not retry this URL; tell the user why and use another source or skip it",
    "rejected": "The approval was rejected: use another source or skip it",
    "expired": "The quote expired: if you still need this data, request it again with a new request_id",
    "fetch_failed": "Fetching the resource failed: nothing was paid and no data was received (error is the reason, not data). You may retry once with a new request_id; if it fails again, use another source or skip it",
    "payment_failed": "The payment did not complete (no money left the wallet): you may retry once with a new request_id; skip it if it fails again",
    "settlement_unknown": "The payment outcome is not known yet: do not buy again with a new request_id; call again later with the same request_id",
    "paid_resource_missing": "Paid but the resource was not delivered: tell the user and do not buy again",
    "in_progress": "This request_id is still being processed: check again later with the same request_id",
    "escrow_in_progress": "Funds are locked in escrow while the data is fetched and verified: check again later with the same request_id",
    "delivery_rejected": "Verification failed: the funds stay in escrow and return to the treasury on expiry; the merchant is not paid. Tell the user why and use another source",
    "refunded": "Verification failed and the funds were refunded to the treasury. Tell the user why and use another source",
    "error": "Invalid request; see reason",
}


def payment_status(r: GateResult) -> str:
    if r.rule_id == "PROBE" and r.state == sm.FAILED:
        return "fetch_failed"
    if r.verdict == "ERROR":
        return "error"
    return {
        sm.RESOURCE_DELIVERED: "free" if r.verdict == "FREE" else "paid",
        sm.PENDING_APPROVAL: "pending_approval",
        sm.DENIED: "denied",
        sm.REJECTED: "rejected",
        sm.EXPIRED: "expired",
        sm.FAILED: "payment_failed",
        sm.SUBMITTED: "settlement_unknown",
        sm.UNKNOWN_SETTLEMENT: "settlement_unknown",
        sm.VALIDATED: "paid_resource_missing",
        sm.ESCROW_LOCKED: "escrow_in_progress",
        sm.DELIVERY_RECEIVED: "escrow_in_progress",
        sm.REFUND_PENDING: "delivery_rejected",
        sm.REFUNDED: "refunded",
    }.get(r.state, "in_progress")


def present(r: GateResult) -> dict[str, Any]:
    status = payment_status(r)
    out: dict[str, Any] = {"status": status, "state": r.state, "verdict": r.verdict, "rule_id": r.rule_id,
                           "reason": r.reason, "url": r.url, "request_id": r.request_id, "replay": r.replay}
    if r.quote:
        out["quote"] = {"amount_xrp": fmt_xrp(r.quote.amount_drops) if r.quote.amount_drops else r.quote.amount,
                        "asset": r.quote.asset, "pay_to": r.quote.pay_to}
    if r.data is not None and status in ("paid", "free"):
        out["content"] = r.data     # 验货不通过的内容不交给 agent
    if r.tx_hash:
        out.update(tx_hash=r.tx_hash, explorer_url=r.explorer_url)
    if r.error and status not in ("paid", "free"):
        out["error"] = r.error
    if r.approval_id:
        out["approval_id"] = r.approval_id
    if status == "pending_approval":
        out["approver_command"] = APPROVE_HINT.format(approval_id=r.approval_id)
    if r.risk:
        out["risk_summary"] = r.risk.get("risk_summary")
    if _NEXT_STEP.get(status):
        out["next_step"] = _NEXT_STEP[status]
    return out


def _escrow_summary(e: dict[str, Any]) -> dict[str, Any]:
    v = e.get("verification") or {}
    return {"status": e["status"], "lock_tx": e.get("create_tx"), "release_tx": e.get("finish_tx"),
            "refund_tx": e.get("cancel_tx"), "refund_in_seconds": e.get("refund_in_seconds"),
            "verification": v.get("verdict"), "verification_reason": v.get("reason"),
            "verification_evidence": v.get("evidence")}


def approval_for_agent(view: dict[str, Any]) -> dict[str, Any]:
    """审批视图 → agent 看到的状态：pending / approved / rejected / expired（附内容或原因）。"""
    status = {"approving": "pending"}.get(view["status"], view["status"])
    out: dict[str, Any] = {"approval_id": view["id"], "status": status, "url": view["url"],
                           "rule_id": view["rule_id"], "hold_reason": view["reason"],
                           "payment_state": view.get("payment_state")}
    if status == "pending":
        out["expires_in_seconds"] = view.get("expires_in_seconds")
        out["approver_command"] = APPROVE_HINT.format(approval_id=view["id"])
    elif status == "approved":
        escrow = view.get("escrow")
        if escrow:
            out["escrow"] = _escrow_summary(escrow)
        if view.get("payment_state") == sm.RESOURCE_DELIVERED:
            out.update(content=view["result"], tx_hash=view["tx_hash"], explorer_url=view["explorer_url"])
        elif view.get("payment_state") in (sm.REFUND_PENDING, sm.REFUNDED):
            out.update(status="delivery_rejected", reason=view.get("payment_error"),
                       next_step=_NEXT_STEP["delivery_rejected"])
        else:
            out.update(error=view.get("payment_error"),
                       next_step="Approved but the payment did not complete; see payment_state. Do not buy again")
    else:
        out.update(reason=view.get("reject_reason"), next_step=_NEXT_STEP.get(status))
    return out
