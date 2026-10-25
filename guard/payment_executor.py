"""付款执行：RESERVED → SIGNING → SUBMITTED → VALIDATED → RESOURCE_DELIVERED。AUTO 和人工批准共用。

前置条件：支付请求处于 RESERVED，预算已经预留（budget.reserve）。

为什么不再用 x402_requests 付 AUTO：它在内部签名并立刻发出，调用方拿不到交易哈希，
响应丢失时就无法按哈希查链；它还会在 facilitator 临时错误时自动重发付款头。
这里改为自己签名（Signer）、先把哈希和 LastLedgerSequence 落库、再发出，并且只发一次。
"""

import asyncio
import json
import threading
import time
from typing import Any, Coroutine

import requests
from x402_xrpl.clients import decode_payment_response
from xrpl.models.transactions import Transaction

from guard import audit, budget, evaluation, payments
from guard import state_machine as sm
from guard.policy import DENY, Decision, fmt_xrp
from guard.quote import Quote, QuoteError, parse_402
from guard.settlement import reconcile
from guard.signers import Signer, verify_signed_blob
from guard.x402_payment import build_payment, decision_memo, payment_header

PROBE_TIMEOUT = 30
BUYER_HEADER = "X-AgentGuard-Buyer"


def buyer_headers(agent_id: str) -> dict[str, str]:
    """告诉商户“这是哪个买方”（agent_id 的哈希，不暴露内部 ID）。商户可以按买方区分会话。"""
    import hashlib
    return {BUYER_HEADER: hashlib.sha256(agent_id.encode()).hexdigest()[:16]}
PAY_TIMEOUT = 180  # 付款要等 facilitator 上链结算，给足时间

AUTO_KIND, APPROVED_KIND = budget.AUTO_KIND, budget.APPROVED_KIND


def execute(payment_id: int, signer: Signer, kind: str, memo: dict[str, Any]) -> dict:
    row = payments.get(payment_id)
    sm.advance(payment_id, sm.SIGNING)

    try:
        fresh = reprobe(row["url"], buyer=row["agent_id"])
    except (requests.RequestException, QuoteError, ValueError) as exc:
        return _fail_before_submit(row, f"付款前重新获取报价失败：{exc}")

    if fresh.terms_hash() != row["quote_hash"]:
        return _deny(row, _swap_decision(row, fresh), fresh)
    recheck = evaluation.decide(fresh)
    if recheck.verdict == DENY:
        return _deny(row, Decision(DENY, recheck.rule_id, f"付款前重新判定为拒绝：{recheck.reason}"), fresh)

    try:
        tx = build_payment(fresh.requirements, signer.address, signer.public_key, [decision_memo(memo)])
        blob = run_sync(signer.sign(tx))
        verify_signed_blob(blob, tx, signer)
    except Exception as exc:  # noqa: BLE001 —— 还没发出任何交易，失败是安全的
        return _fail_before_submit(row, f"构造或签名交易失败：{exc}")

    tx_hash = Transaction.from_blob(blob).get_hash()
    # 先落库再发出：之后无论发生什么，都能按这个哈希查链
    sm.advance(payment_id, sm.SUBMITTED, tx_hash=tx_hash,
               last_ledger_sequence=tx.last_ledger_sequence, submitted_at=time.time())

    header = payment_header(fresh.requirements, blob, fresh.resource)
    try:
        resp = requests.get(row["url"], headers={"PAYMENT-SIGNATURE": header, **buyer_headers(row["agent_id"])},
                            timeout=PAY_TIMEOUT)
    except requests.RequestException as exc:
        return _unknown(payment_id, f"签名交易已发出，但没有收到商户响应：{exc}")

    settled_hash, settle_error = settlement_from_response(resp)
    if resp.status_code == 200 and not settle_error and settled_hash == tx_hash:
        return _delivered(row, tx_hash, body(resp), kind)
    detail = settle_error or f"HTTP {resp.status_code}：{body(resp)}"
    if settled_hash and settled_hash != tx_hash:
        detail += f"（商户返回的交易哈希 {settled_hash} 与我们签名的 {tx_hash} 不一致）"
    return _unknown(payment_id, f"结算结果不明：{detail}")


def reprobe(url: str, scheme: str | None = None, buyer: str | None = None) -> Quote:
    """重新拿报价；给了 scheme 就选同一付款方式的选项（没有该选项时原样返回，由 quote_hash 比对拦下）。"""
    resp = requests.get(url, headers=buyer_headers(buyer) if buyer else None, timeout=PROBE_TIMEOUT,
                        allow_redirects=False)
    if resp.status_code != 402:
        raise ValueError(f"商户返回 HTTP {resp.status_code}，不是 402")
    quote = parse_402(resp)
    return (quote.with_scheme(scheme) or quote) if scheme else quote


def settlement_from_response(resp: requests.Response) -> tuple[str | None, str | None]:
    """从 PAYMENT-RESPONSE 头取交易哈希；返回 (tx_hash, 错误原因)。"""
    header = resp.headers.get("PAYMENT-RESPONSE")
    if not header:
        return None, None if resp.status_code == 200 else "缺少 PAYMENT-RESPONSE"
    try:
        settled = decode_payment_response(header)
    except ValueError as exc:
        return None, str(exc)
    if settled.get("success") is False:
        return settled.get("transaction") or None, settled.get("errorReason") or "settlement_failed"
    return settled.get("transaction") or None, None


def body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def run_sync(coro: Coroutine) -> Any:
    """在同步代码里等待 Signer.sign()。当前线程已有事件循环时，换一个线程跑。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(result=asyncio.run(coro)))
    thread.start()
    thread.join()
    return box["result"]


# ---- 结局 ----

def _delivered(row: dict, tx_hash: str, data: Any, kind: str) -> dict:
    sm.advance(row["id"], sm.VALIDATED)
    budget.spend(row["id"])
    sm.advance(row["id"], sm.RESOURCE_DELIVERED, result_json=json.dumps(data, ensure_ascii=False))
    if kind == AUTO_KIND:
        audit.mark_paid(row["decision_id"], tx_hash)
    else:
        audit.mark_approved_paid(row["decision_id"], tx_hash)
    return payments.get(row["id"])


def _unknown(payment_id: int, error: str) -> dict:
    sm.advance(payment_id, sm.UNKNOWN_SETTLEMENT, error=error)
    return reconcile(payment_id)   # 立即查一次链；查不到就保持未知，绝不重发


def _fail_before_submit(row: dict, error: str) -> dict:
    sm.advance(row["id"], sm.FAILED, error=error)
    budget.release(row["id"])
    audit.mark_failed(row["decision_id"], error)
    return payments.get(row["id"])


def _deny(row: dict, decision: Decision, fresh: Quote) -> dict:
    deny_id = audit.log_decision(row["url"], row["purpose"], fresh, decision, status="denied")
    audit.mark_aborted(row["decision_id"], f"付款前被拦截，见决策 #{deny_id}")
    sm.advance(row["id"], sm.DENIED, verdict=DENY, rule_id=decision.rule_id, reason=decision.reason,
               decision_id=deny_id)
    budget.release(row["id"])
    return payments.get(row["id"])


def _swap_decision(row: dict, fresh: Quote) -> Decision:
    return Decision(DENY, "R8", (
        f"报价在判定和付款之间被调包：{fmt_xrp(row['amount_drops'])} XRP → {row['pay_to']} "
        f"变成 {fmt_xrp(fresh.amount_drops) if fresh.amount_drops else fresh.amount} {fresh.asset} → {fresh.pay_to}，已拒绝付款"))
