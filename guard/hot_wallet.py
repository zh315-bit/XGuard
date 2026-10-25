"""热钱包余额封顶：私钥在服务器上，余额上限 hot_wallet_max_balance_xrp 是服务器能直接动用的上限。

- cap_excess()：超过上限的部分转回金库（热钱包自己签名，带审计 Memo）
- request_topup()：补款只能由金库付款 → 生成一张审批单，必须人工批准 + 设备签名
- execute_topup()：批准后执行；付款前用最新余额再查一次，补完不得超过上限
"""

import json
import time
import uuid
from typing import Any, Optional

from x402_xrpl.types import PaymentRequirements
from xrpl.models.transactions import Payment, Transaction

from guard import approvals, audit, chain, payments, policy, risk_explainer
from guard import state_machine as sm
from guard.policy import HOLD, Decision, fmt_xrp
from guard.quote import Quote
from guard.signers import Signer, verify_signed_blob
from guard.x402_payment import decision_memo
from guard.xrpl_client import address_of, explorer_tx_url, load_wallet

TOPUP_SCHEME = "topup"
TOPUP_URL = "xrpl://treasury/topup-hot-wallet"
OPERATOR = "operator"


class CapExceeded(ValueError):
    pass


def status(cfg: Optional[policy.PolicyConfig] = None) -> dict[str, Any]:
    cfg = cfg or policy.load_policy()
    balance = chain.balance_drops(address_of("agent"))
    return {"balance_drops": balance, "cap_drops": cfg.hot_wallet_max_balance_drops,
            "balance_xrp": fmt_xrp(balance), "cap_xrp": fmt_xrp(cfg.hot_wallet_max_balance_drops),
            "excess_xrp": fmt_xrp(max(0, balance - cfg.hot_wallet_max_balance_drops)),
            "headroom_xrp": fmt_xrp(max(0, cfg.hot_wallet_max_balance_drops - balance))}


def excess_to_return(balance_drops: int, cap_drops: int) -> int:
    """要转回金库的金额。手续费从热钱包另付，所以转完后余额 = 上限 - 手续费 < 上限。"""
    return max(0, balance_drops - cap_drops)


def cap_excess() -> Optional[dict[str, Any]]:
    """把超过上限的部分转回金库。没有超出返回 None。"""
    cfg = policy.load_policy()
    agent = load_wallet("agent")
    balance = chain.balance_drops(agent.address)
    amount = excess_to_return(balance, cfg.hot_wallet_max_balance_drops)
    if amount == 0:
        return None
    memo = {"action": "hot-wallet-cap", "cap_xrp": fmt_xrp(cfg.hot_wallet_max_balance_drops),
            "balance_before_xrp": fmt_xrp(balance), "returned_xrp": fmt_xrp(amount)}
    tx = Payment(account=agent.address, destination=address_of("treasury"), amount=str(amount),
                 memos=[decision_memo(memo, memo_type="agentguard/hot-wallet")])
    result = chain.submit_with_wallet(tx, agent)
    return {"result": chain.result_code(result), "tx_hash": result["hash"], "explorer_url": explorer_tx_url(result["hash"]),
            "returned_xrp": fmt_xrp(amount), "balance_after_xrp": fmt_xrp(chain.balance_drops(agent.address))}


def request_topup(amount_drops: int, reason: str) -> dict[str, Any]:
    """创建补款审批单。补完会超过上限的请求直接拒绝，不会生成审批单。"""
    from guard import lockdown
    lockdown.ensure_unlocked()
    cfg = policy.load_policy()
    if amount_drops <= 0:
        raise CapExceeded("The top-up amount must be greater than 0")
    balance = chain.balance_drops(address_of("agent"))
    _check_cap(balance, amount_drops, cfg)

    agent_address = address_of("agent")
    req = PaymentRequirements(scheme=TOPUP_SCHEME, network=cfg.network, amount=str(amount_drops), asset="XRP",
                              pay_to=agent_address, max_timeout_seconds=600,
                              extra={"invoiceId": uuid.uuid4().hex.upper(), "description": "Treasury top-up to the hot wallet"})
    quote = Quote(TOPUP_URL, req)
    decision = Decision(HOLD, "TOPUP", f"Hot wallet top-up {fmt_xrp(amount_drops)} XRP: balance {fmt_xrp(balance)}, "
                                       f"after top-up {fmt_xrp(balance + amount_drops)} / cap {fmt_xrp(cfg.hot_wallet_max_balance_drops)} XRP")
    row, _ = payments.create_or_get(OPERATOR, f"topup-{uuid.uuid4().hex[:12]}", TOPUP_URL, reason)
    pid = row["id"]
    sm.advance(pid, sm.QUOTED, quote_json=json.dumps(req.to_dict()), quote_hash=quote.terms_hash(),
               amount_drops=amount_drops, pay_to=agent_address, method=TOPUP_SCHEME)
    decision_id = audit.log_decision(TOPUP_URL, reason, quote, decision, status="pending_approval")
    sm.advance(pid, sm.EVALUATED, verdict=HOLD, rule_id="TOPUP", reason=decision.reason, decision_id=decision_id)
    risk = risk_explainer.template_explain({"rule_id": "TOPUP", "verdict": HOLD, "rule_reason": decision.reason,
                                            "amount_xrp": fmt_xrp(amount_drops), "asset": "XRP",
                                            "network": cfg.network, "pay_to": agent_address})
    risk = {**risk, "source": "template"}
    approval_id = approvals.enqueue(quote, reason, decision, decision_id, pid, cfg.approval_ttl_seconds, risk)
    sm.advance(pid, sm.PENDING_APPROVAL, approval_id=approval_id, risk_json=json.dumps(risk, ensure_ascii=False))
    return {"approval_id": approval_id, "payment_id": pid, "reason": decision.reason}


def execute_topup(payment_id: int, signer: Signer, memo: dict[str, Any]) -> dict:
    """批准后执行补款（RESERVED → SIGNING → SUBMITTED → VALIDATED → RESOURCE_DELIVERED）。"""
    from guard.payment_executor import _fail_before_submit, _unknown, run_sync   # 避免循环导入
    row = payments.get(payment_id)
    sm.advance(payment_id, sm.SIGNING)
    cfg = policy.load_policy()
    try:
        _check_cap(chain.balance_drops(row["pay_to"]), row["amount_drops"], cfg)   # 用最新余额再查一次
        tx = chain.prepare(Payment(account=signer.address, destination=row["pay_to"], amount=str(row["amount_drops"]),
                                   signing_pub_key=signer.public_key, memos=[decision_memo(memo)]))
        blob = run_sync(signer.sign(tx))
        verify_signed_blob(blob, tx, signer)
    except Exception as exc:  # noqa: BLE001 —— 还没发出任何交易
        return _fail_before_submit(row, f"Top-up not executed: {exc}")

    tx_hash = Transaction.from_blob(blob).get_hash()
    sm.advance(payment_id, sm.SUBMITTED, tx_hash=tx_hash, last_ledger_sequence=tx.last_ledger_sequence,
               submitted_at=time.time())
    try:
        result = chain.submit_signed(blob)
    except chain.SubmitUnknown as exc:
        return _unknown(payment_id, str(exc))
    if chain.result_code(result) != "tesSUCCESS":
        return _unknown(payment_id, f"Top-up transaction result {chain.result_code(result)}")
    sm.advance(payment_id, sm.VALIDATED)
    new_balance = chain.balance_drops(row["pay_to"])
    sm.advance(payment_id, sm.RESOURCE_DELIVERED,
               result_json=json.dumps({"topup_xrp": fmt_xrp(row["amount_drops"]), "hot_wallet_balance_xrp": fmt_xrp(new_balance)}))
    audit.mark_approved_paid(row["decision_id"], tx_hash)
    return payments.get(payment_id)


def _check_cap(balance_drops: int, amount_drops: int, cfg: policy.PolicyConfig) -> None:
    if balance_drops + amount_drops > cfg.hot_wallet_max_balance_drops:
        raise CapExceeded(f"After the top-up the hot wallet would hold {fmt_xrp(balance_drops + amount_drops)} XRP, above the cap of "
                          f"{fmt_xrp(cfg.hot_wallet_max_balance_drops)} XRP (balance {fmt_xrp(balance_drops)})")

