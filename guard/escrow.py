"""先验货再放款：大额购买用 XRPL 条件式 Escrow 代替 x402 的“先付款后拿货”。

流程（payment_requests.method = "escrow"，审批通过后执行）：
  1. 锁款：金库 EscrowCreate 给商户，PREIMAGE-SHA-256 条件 + CancelAfter，由设备签名；守卫保存原像
  2. 取货：带 escrow 付款证明（AgentGuard 对 x402 的扩展，不是标准 x402）请求商户：
       X-ESCROW-OWNER / X-ESCROW-SEQUENCE / X-ESCROW-TX
     商户在链上确认 escrow 存在、收款人是自己、金额对、带条件，然后交付内容
  3. 验货：delivery_verifier（确定性检查 + LLM）
  4. 通过 → EscrowFinish（提交原像）放款；不通过 → 等 CancelAfter 过后 EscrowCancel 退款
EscrowFinish / EscrowCancel 任何账户都能提交，这里由热钱包提交（只付手续费），不需要金库的钥匙。
三笔交易都带审计 Memo；Finish / Cancel 的 Memo 里有验货结论的哈希。
"""

import hashlib
import json
import os
import time
from typing import Any, Optional

import requests
from xrpl.models.transactions import EscrowCancel, EscrowCreate, EscrowFinish, Transaction
from xrpl.utils import ripple_time_to_posix

from guard import audit, budget, chain, delivery_verifier, evaluation, payments, policy
from guard import state_machine as sm
from guard.db import connect
from guard.policy import DENY, Decision
from guard.signers import Signer, verify_signed_blob
from guard.x402_payment import decision_memo
from guard.xrpl_client import explorer_tx_url, load_wallet

ESCROW_SCHEME = "escrow"
MEMO_TYPE = "agentguard/escrow"
DELIVERY_TIMEOUT = 60


class TooEarlyToCancel(RuntimeError):
    pass


# ---- 条件 / 原像 ----

def new_condition() -> tuple[str, str]:
    """返回 (原像 hex, 条件 hex)。条件按 crypto-conditions 的 PREIMAGE-SHA-256 编码（已在 testnet 实测）。"""
    preimage = os.urandom(32)
    return preimage.hex().upper(), condition_for(preimage.hex())


def condition_for(preimage_hex: str) -> str:
    return "A0258020" + hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest().upper() + "810120"


def fulfillment_for(preimage_hex: str) -> str:
    return "A0228020" + preimage_hex.upper()


# ---- escrows 表 ----

def create_record(payment_id: int, approval_id: Optional[int], **fields: Any) -> int:
    cols = ["payment_request_id", "approval_id", "created_at", "status", *fields]
    values = [payment_id, approval_id, time.time(), "preparing", *fields.values()]
    with connect() as conn:
        return conn.execute(f"INSERT INTO escrows ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            values).lastrowid


def get(payment_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM escrows WHERE payment_request_id = ?", (payment_id,)).fetchone()
    return dict(row) if row else None


def update(payment_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE escrows SET {cols} WHERE payment_request_id = ?", (*fields.values(), payment_id))


def claim(payment_id: int, from_status: str, to_status: str) -> bool:
    """原子地改状态（防止两个清理任务同时放款 / 退款）。"""
    with connect() as conn:
        cur = conn.execute("UPDATE escrows SET status = ? WHERE payment_request_id = ? AND status = ?",
                           (to_status, payment_id, from_status))
        return cur.rowcount == 1


def list_all(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM escrows ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def public_view(record: dict) -> dict[str, Any]:
    """给控制台 / agent 看的视图：绝不包含原像。"""
    hidden = ("preimage", "delivery_json", "verification_json", "data_schema")
    out = {k: v for k, v in record.items() if k not in hidden}
    for key, name in (("delivery_json", "delivery"), ("verification_json", "verification"), ("data_schema", "data_schema")):
        out[name] = json.loads(record[key]) if record.get(key) else None
    payment = payments.get(record["payment_request_id"])
    out["payment_state"] = payment["state"] if payment else None
    out["payment_error"] = payment.get("error") if payment else None
    if out["status"] == "preparing" and out["payment_state"] in (sm.FAILED, sm.DENIED, sm.EXPIRED, sm.REJECTED):
        out["status"] = "failed"     # 兜底：旧记录或异常路径，状态以支付请求为准
    out["cancel_after_unix"] = ripple_time_to_posix(record["cancel_after"])
    out["refund_in_seconds"] = max(0, int(out["cancel_after_unix"] - time.time()))
    for tx in ("create_tx", "finish_tx", "cancel_tx"):
        out[f"{tx}_url"] = explorer_tx_url(record[tx]) if record.get(tx) else None
    return out


# ---- 1. 锁款 ----

def execute(payment_id: int, signer: Signer, memo: dict[str, Any]) -> dict:
    """批准后执行：RESERVED → SIGNING → SUBMITTED → ESCROW_LOCKED → 取货、验货 → 放款或待退款。"""
    from guard.payment_executor import _deny, _fail_before_submit, _swap_decision, _unknown, reprobe, run_sync
    row = payments.get(payment_id)
    sm.advance(payment_id, sm.SIGNING)
    try:
        fresh = reprobe(row["url"], ESCROW_SCHEME, buyer=row["agent_id"])
    except (requests.RequestException, ValueError) as exc:
        return _fail_before_submit(row, f"Re-fetching the quote before locking failed: {exc}")
    if fresh.terms_hash() != row["quote_hash"]:
        return _deny(row, _swap_decision(row, fresh), fresh)
    recheck = evaluation.decide(fresh)
    if recheck.verdict == DENY:
        return _deny(row, Decision(DENY, recheck.rule_id, f"Re-evaluated as denied before locking: {recheck.reason}"), fresh)

    cfg = policy.load_policy()
    preimage, condition = new_condition()
    try:
        cancel_after = chain.ledger_close_time() + cfg.escrow_cancel_after_seconds
        # 验收条件只用审批单快照（审批人批准的那份），不用商户这次再给的描述
        terms = _approved_acceptance(row) or fresh.acceptance()
        description = terms.get("description") or terms.get("resourceDescription")
        create_record(payment_id, row["approval_id"], owner=signer.address, destination=fresh.pay_to,
                      amount_drops=fresh.amount_drops, condition=condition, preimage=preimage,
                      cancel_after=cancel_after, description=description,
                      data_schema=json.dumps(terms["dataSchema"]) if terms.get("dataSchema") else None)
        tx = chain.prepare(EscrowCreate(
            account=signer.address, destination=fresh.pay_to, amount=str(fresh.amount_drops),
            condition=condition, cancel_after=cancel_after, signing_pub_key=signer.public_key,
            memos=[decision_memo({**memo, "escrow": "lock"})]))
        blob = run_sync(signer.sign(tx))
        verify_signed_blob(blob, tx, signer)
    except Exception as exc:  # noqa: BLE001 —— 还没发出任何交易
        if get(payment_id):
            update(payment_id, status="failed")      # 卡片状态与支付状态一致：没锁款、没动钱
        return _fail_before_submit(row, f"Lock not executed: {exc}")

    tx_hash = Transaction.from_blob(blob).get_hash()
    sm.advance(payment_id, sm.SUBMITTED, tx_hash=tx_hash, last_ledger_sequence=tx.last_ledger_sequence,
               submitted_at=time.time())
    update(payment_id, create_tx=tx_hash, offer_sequence=tx.sequence)
    try:
        result = chain.submit_signed(blob)
    except chain.SubmitUnknown as exc:
        return _unknown(payment_id, str(exc))
    if chain.result_code(result) != "tesSUCCESS":
        return _unknown(payment_id, f"EscrowCreate result {chain.result_code(result)}")
    mark_locked(payment_id)
    return deliver_and_verify(payment_id)


def _approved_acceptance(row: dict) -> Optional[dict]:
    from guard import approvals
    approval = approvals.get(row["approval_id"]) if row.get("approval_id") else None
    return approvals.acceptance(approval) if approval else None


def mark_locked(payment_id: int) -> None:
    sm.advance(payment_id, sm.ESCROW_LOCKED)
    update(payment_id, status="locked")


# ---- 2. 取货 + 3. 验货 ----

def deliver_and_verify(payment_id: int) -> dict:
    row, record = payments.get(payment_id), get(payment_id)
    from guard.payment_executor import buyer_headers
    headers = {"X-ESCROW-OWNER": record["owner"], "X-ESCROW-SEQUENCE": str(record["offer_sequence"]),
               "X-ESCROW-TX": record["create_tx"], **buyer_headers(row["agent_id"])}
    try:
        resp = requests.get(row["url"], headers=headers, timeout=DELIVERY_TIMEOUT)
        delivery = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        return to_refund(payment_id, f"The merchant did not deliver: {exc}")
    if delivery is None:
        return to_refund(payment_id, f"The merchant did not deliver: HTTP {resp.status_code} {resp.text[:200]}")

    delivery_text = json.dumps(delivery, ensure_ascii=False)
    sm.advance(payment_id, sm.DELIVERY_RECEIVED, result_json=delivery_text)
    update(payment_id, delivery_json=delivery_text, status="delivered")

    schema = json.loads(record["data_schema"]) if record.get("data_schema") else None
    verification = delivery_verifier.verify(record["description"] or "", schema, delivery)
    update(payment_id, verification_json=json.dumps(verification, ensure_ascii=False),
           verification_hash=delivery_verifier.verification_hash(verification))
    if verification["verdict"] == delivery_verifier.PASS:
        return release(payment_id)
    return to_refund(payment_id, f"Verification failed: {verification['reason']}")


# ---- 4a. 放款 ----

def release(payment_id: int) -> dict:
    """验货通过：用原像执行 EscrowFinish。失败时回到 delivered，由清理任务在 CancelAfter 之前重试。"""
    record = get(payment_id)
    if not claim(payment_id, "delivered", "releasing"):
        return payments.get(payment_id)
    wallet = load_wallet("agent")
    tx = EscrowFinish(account=wallet.address, owner=record["owner"], offer_sequence=record["offer_sequence"],
                      condition=record["condition"], fulfillment=fulfillment_for(record["preimage"]),
                      memos=[_outcome_memo(record, "release")])
    try:
        result = chain.submit_with_wallet(tx, wallet)
    except Exception as exc:  # noqa: BLE001
        update(payment_id, status="delivered")
        payments.set_error(payment_id, f"Submitting the release failed, will retry: {exc}")
        return payments.get(payment_id)
    if chain.result_code(result) != "tesSUCCESS":
        update(payment_id, status="delivered")
        payments.set_error(payment_id, f"Release failed ({chain.result_code(result)}), will retry")
        return payments.get(payment_id)

    update(payment_id, status="released", finish_tx=result["hash"])
    sm.advance(payment_id, sm.RESOURCE_DELIVERED)
    budget.spend(payment_id)
    audit.mark_approved_paid(payments.get(payment_id)["decision_id"], result["hash"])
    return payments.get(payment_id)


# ---- 4b. 退款 ----

def to_refund(payment_id: int, reason: str) -> dict:
    """标记为待退款。钱还锁在 escrow 里，CancelAfter 之后由清理任务退回金库。"""
    sm.advance(payment_id, sm.REFUND_PENDING, error=reason)
    update(payment_id, status="refund_pending")
    return payments.get(payment_id)


def refund(payment_id: int) -> dict:
    """EscrowCancel。CancelAfter 之前调用抛 TooEarlyToCancel（链上也会拒绝，这里提前拦下，不浪费手续费）。"""
    record = get(payment_id)
    now = chain.ledger_close_time()
    if now <= record["cancel_after"]:
        raise TooEarlyToCancel(f"CancelAfter not reached yet: {record['cancel_after'] - now} s to go")
    if not claim(payment_id, "refund_pending", "refunding"):
        return payments.get(payment_id)
    wallet = load_wallet("agent")
    tx = EscrowCancel(account=wallet.address, owner=record["owner"], offer_sequence=record["offer_sequence"],
                      memos=[_outcome_memo(record, "refund")])
    try:
        result = chain.submit_with_wallet(tx, wallet)
        code = chain.result_code(result)
    except Exception as exc:  # noqa: BLE001
        code, result = f"error: {exc}", {}
    if code != "tesSUCCESS":
        update(payment_id, status="refund_pending")
        payments.set_error(payment_id, f"{payments.get(payment_id).get('error')}; submitting the refund failed ({code}), will retry")
        return payments.get(payment_id)

    update(payment_id, status="refunded", cancel_tx=result["hash"])
    sm.advance(payment_id, sm.REFUNDED)
    budget.release(payment_id)
    audit.mark_refunded(payments.get(payment_id)["decision_id"], result["hash"])
    return payments.get(payment_id)


def sweep() -> None:
    """清理任务：到期的待退款执行退款；放款失败的在 CancelAfter 前重试。"""
    for record in list_all(200):
        try:
            if record["status"] == "refund_pending" and chain.ledger_close_time() > record["cancel_after"]:
                refund(record["payment_request_id"])
            elif record["status"] == "delivered" and _passed(record):
                release(record["payment_request_id"])
        except Exception:  # noqa: BLE001 —— 下次再试
            continue


def _passed(record: dict) -> bool:
    v = json.loads(record["verification_json"]) if record.get("verification_json") else {}
    return v.get("verdict") == delivery_verifier.PASS


def _outcome_memo(record: dict, action: str):
    verification = json.loads(record["verification_json"]) if record.get("verification_json") else {}
    return decision_memo({"escrow": action, "approval_id": record["approval_id"],
                          "verification": verification.get("verdict", "none"),
                          "verification_hash": record.get("verification_hash")}, memo_type=MEMO_TYPE)
