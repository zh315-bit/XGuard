"""审批流程：批准 = 检查过期 → 原子预留预算（硬上限 + 每日总上限）→ 金库签名付款；拒绝 = 记录原因。

- 批准是一次性的：approvals.try_lock 原子地把 pending 变成 approving，只有一个调用者能拿到；
  之后的重复调用一律 409，返回已有结果，绝不会再付款
- 审批单绑定 quote_hash 和 expires_at：过期 → EXPIRED；付款前重新拿到的报价哈希不一致 → R8 DENIED
- 人工批准不能突破硬上限（R7）和每日总上限：budget.reserve 失败时拒绝批准并写明原因
- SIGNER_MODE=speculos：批准后在后台执行付款，签名步骤等前端 / CLI 用设备签好并提交 blob
  （submit_signature）；approve() 在签名请求就绪后立即返回，不会让 HTTP 请求挂几分钟
"""

import asyncio
import json
import time
from typing import Any

from guard import approvals, audit, budget, lockdown, maintenance, payment_executor, payments, policy, signing_requests
from guard import state_machine as sm
from guard.budget import BudgetExceeded
from guard.signers import ExternalSigner, Signer, approval_signer
from guard.xrpl_client import explorer_tx_url

# 后台付款任务的强引用（否则可能被 GC 回收）
_background: set[asyncio.Task] = set()
SIGNING_REQUEST_WAIT_SECONDS = 15


class ApprovalError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


UNVERIFIED_PAYEE_RULE = "R3"


def payee_confirmation_required(approval: dict) -> bool:
    """收款方没有有效凭证（R3）：可能是钓鱼地址，批准前必须手动输入收款地址后 4 位。"""
    return approval["rule_id"] == UNVERIFIED_PAYEE_RULE


async def approve(approval_id: int, signer: Signer | None = None,
                  confirm_payee_suffix: str | None = None) -> dict[str, Any]:
    """signer 为 None 时按 SIGNER_MODE 选择（测试可以直接传入）。
    R3 审批单必须带 confirm_payee_suffix（收款地址后 4 位，区分大小写），否则拒绝执行批准。"""
    maintenance.sweep()
    approval = _require_pending(approval_id)
    if lockdown.is_locked():
        raise ApprovalError(423, f"Cannot approve: {lockdown.REASON}")
    if payee_confirmation_required(approval):
        pay_to = json.loads(approval["quote_json"])["payTo"]
        if (confirm_payee_suffix or "").strip() != pay_to[-4:]:
            raise ApprovalError(400, "Unverified payee (R3), possibly a phishing address: type the last 4 characters of the payee address to confirm")
    if not approvals.try_lock(approval_id):
        raise ApprovalError(409, f"Approval #{approval_id} is being processed or already handled; approval is one-time and will not pay again")
    payment = payments.get(approval["payment_request_id"])

    if time.time() > approval["expires_at"]:
        _expire(approval, "The quote has expired and can no longer be approved; ask the agent to request again")
        return view(approval_id)

    cfg = policy.load_policy()
    if payment.get("method") == "topup":
        # 补款是金库到热钱包的内部划转，不算支出、不占日预算；上限检查在 execute_topup 里用最新余额做
        sm.advance(payment["id"], sm.RESERVED)
    else:
        try:
            budget.reserve(payment["id"], payment["amount_drops"], budget.APPROVED_KIND, cfg,
                           from_state=sm.PENDING_APPROVAL)
        except BudgetExceeded as exc:
            _reject(approval, f"Cannot approve ({exc.decision.rule_id}): {exc.decision.reason}")
            return view(approval_id)

    signer = signer or approval_signer(approval_id, payment["id"], cfg.approval_ttl_seconds)
    memo = {"verdict": "HOLD", "rule_id": approval["rule_id"], "approval_id": approval_id}
    task = asyncio.create_task(_execute_and_finish(approval_id, payment["id"], signer, memo, payment.get("method")))
    if not isinstance(signer, ExternalSigner):
        await task
        return view(approval_id)

    _background.add(task)
    task.add_done_callback(_background.discard)
    # 外部签名：等到签名请求生成（或付款前就已结束，比如报价被调包）再返回
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SIGNING_REQUEST_WAIT_SECONDS
    while not task.done() and signing_requests.for_approval(approval_id) is None and loop.time() < deadline:
        await asyncio.sleep(0.2)
    return view(approval_id)


def submit_signature(approval_id: int, signed_blob: str) -> dict[str, Any]:
    """前端 / CLI 提交设备签好的 blob。校验不通过 → 400，这次付款失败，不会发出任何交易。"""
    approval = approvals.get(approval_id)
    if approval is None:
        raise ApprovalError(404, f"Approval #{approval_id} does not exist")
    if lockdown.is_locked():
        raise ApprovalError(423, f"Signature not accepted: {lockdown.REASON}")
    try:
        signing_requests.submit(approval, signed_blob)
    except signing_requests.SignatureRejected as exc:
        raise ApprovalError(400, f"Signature rejected: {exc}") from exc
    return view(approval_id)


def cancel_signature(approval_id: int, reason: str) -> dict[str, Any]:
    """设备上拒绝签名，或审批人放弃签名。"""
    signing_requests.cancel(approval_id, reason)
    return view(approval_id)


def _executor(method: str | None):
    """付款方式 → 执行器：x402 直接付款 / escrow 锁款验货 / topup 金库给热钱包补款。"""
    from guard import escrow, hot_wallet
    if method == "escrow":
        return lambda pid, signer, memo: escrow.execute(pid, signer, memo)
    if method == "topup":
        return lambda pid, signer, memo: hot_wallet.execute_topup(pid, signer, memo)
    return lambda pid, signer, memo: payment_executor.execute(pid, signer, budget.APPROVED_KIND, memo)


async def _execute_and_finish(approval_id: int, payment_id: int, signer: Signer, memo: dict,
                              method: str | None = None) -> None:
    try:
        row = await asyncio.to_thread(_executor(method), payment_id, signer, memo)
    except Exception as exc:
        approvals.mark_approved(approval_id, None, None, error=f"Payment execution error: {exc}")
        raise ApprovalError(502, f"Payment execution error after approval: {exc}") from exc
    if row["state"] == sm.DENIED:
        approvals.mark_rejected(approval_id, f"Automatically rejected: {row['reason']}")
    elif row["state"] == sm.FAILED:
        # 付款前就失败（例如设备上拒绝签名、签名校验不通过）：钱没有动，按拒绝处理
        approvals.mark_rejected(approval_id, f"Payment not completed: {row.get('error')}")
    else:
        approvals.mark_approved(approval_id, row.get("tx_hash"), payments.result(row),
                                error=None if row["state"] == sm.RESOURCE_DELIVERED else row.get("error"))


def reject(approval_id: int, reason: str) -> dict[str, Any]:
    maintenance.sweep()
    approval = _require_pending(approval_id)
    if not approvals.try_lock(approval_id):
        raise ApprovalError(409, f"Approval #{approval_id} is being processed or already handled")
    _reject(approval, reason)
    return view(approval_id)


def view(approval_id: int) -> dict[str, Any]:
    """审批单 + 对应支付请求的状态。API、CLI、MCP 都用这个视图。"""
    a = approvals.get(approval_id)
    if a is None:
        raise ApprovalError(404, f"Approval #{approval_id} does not exist")
    p = payments.get(a["payment_request_id"]) if a.get("payment_request_id") else None
    out = {k: v for k, v in a.items() if k not in ("quote_json", "result_json", "risk_json", "snapshot_json")}
    out["acceptance"] = approvals.acceptance(a) or None
    out["quote"] = json.loads(a["quote_json"])
    out["result"] = json.loads(a["result_json"]) if a.get("result_json") else None
    out["risk"] = json.loads(a["risk_json"]) if a.get("risk_json") else None
    out["expires_in_seconds"] = max(0, int(a["expires_at"] - time.time())) if a.get("expires_at") else None
    out["payment_state"] = p["state"] if p else None
    out["payment_error"] = p.get("error") if p else None
    out["explorer_url"] = explorer_tx_url(a["tx_hash"]) if a.get("tx_hash") else None
    req = signing_requests.for_approval(approval_id)
    out["signing_request"] = signing_requests.summary(req) if req else None
    out["method"] = p.get("method") if p else "x402"
    out["payee_unverified"] = payee_confirmation_required(a)
    if out["method"] == "escrow":
        from guard import escrow
        record = escrow.get(p["id"])
        out["escrow"] = escrow.public_view(record) if record else None
    out["budget_impact"] = _budget_impact(a)
    return out


def _budget_impact(a: dict) -> dict[str, Any]:
    cfg = policy.load_policy()
    amount = int(json.loads(a["quote_json"])["amount"])
    total = budget.committed_today()
    return {"amount_xrp": policy.fmt_xrp(amount), "total_committed_today_xrp": policy.fmt_xrp(total),
            "daily_total_cap_xrp": policy.fmt_xrp(cfg.daily_total_cap_drops),
            "after_approval_xrp": policy.fmt_xrp(total + amount) if a["status"] == "pending" else None,
            "within_cap": a["status"] != "pending" or total + amount <= cfg.daily_total_cap_drops}


def _require_pending(approval_id: int) -> dict:
    approval = approvals.get(approval_id)
    if approval is None:
        raise ApprovalError(404, f"Approval #{approval_id} does not exist")
    if approval["status"] != "pending":
        raise ApprovalError(409, f"Approval #{approval_id} is currently {approval['status']}; "
                                 f"approval is one-time and will not pay again")
    return approval


def _reject(approval: dict, reason: str) -> None:
    approvals.mark_rejected(approval["id"], reason)
    audit.mark_rejected(approval["decision_id"], reason)
    sm.advance(approval["payment_request_id"], sm.REJECTED, error=reason)


def _expire(approval: dict, reason: str) -> None:
    approvals.mark_expired(approval["id"], reason)
    audit.mark_expired(approval["decision_id"], reason)
    sm.advance(approval["payment_request_id"], sm.EXPIRED, error=reason)
