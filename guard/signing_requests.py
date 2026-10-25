"""外部签名请求（Ledger / Speculos）：后端准备未签名交易 → 前端用设备签名 → 提交签好的 blob。

提交的 blob 在被接受之前必须全部通过：
- 与待签交易逐字段一致（只允许多出 TxnSignature）
- 收款地址、金额、资产与审批单的报价一致，按这三项重新计算的 quote_hash 等于审批单绑定的 quote_hash
- SigningPubKey 是设备公钥、Account 是金库，签名能用该公钥验证通过
任何一项不通过：拒绝这次签名（签名请求标为 rejected），付款不会发出。
"""

import json
import time
from typing import Any, Optional

from xrpl.core.binarycodec import decode, encode_for_signing
from xrpl.core.keypairs import is_valid_message

from guard.db import connect

PENDING, SIGNED, REJECTED, EXPIRED = "pending", "signed", "rejected", "expired"


class SignatureRejected(ValueError):
    pass


def create(approval_id: int, payment_id: int, signer_account: str, signing_pub_key: str,
           unsigned_hex: str, ttl_seconds: float) -> int:
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO signing_requests (approval_id, payment_request_id, created_at, expires_at, signer_account,
                                             signing_pub_key, unsigned_hex, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (approval_id, payment_id, now, now + ttl_seconds, signer_account, signing_pub_key, unsigned_hex, PENDING))
        return cur.lastrowid


def get(request_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM signing_requests WHERE id = ?", (request_id,)).fetchone()
    return dict(row) if row else None


def for_approval(approval_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM signing_requests WHERE approval_id = ? ORDER BY id DESC LIMIT 1",
                           (approval_id,)).fetchone()
    return dict(row) if row else None


def summary(req: dict) -> dict[str, Any]:
    """给前端 / CLI：要签的字节、交易 JSON（前端用 xrpl.js encode 组装签好的 blob）和一句话摘要。"""
    tx = decode(req["unsigned_hex"])
    return {
        "id": req["id"], "status": req["status"], "error": req.get("error"),
        "unsigned_tx_hex": req["unsigned_hex"], "tx_json": tx,
        "destination": tx.get("Destination"), "amount_drops": tx.get("Amount"),
        "account": tx.get("Account"), "signing_pub_key": tx.get("SigningPubKey"),
        "last_ledger_sequence": tx.get("LastLedgerSequence"),
        "expires_in_seconds": max(0, int(req["expires_at"] - time.time())),
    }


def submit(approval: dict, signed_blob: str) -> dict:
    """校验并接受签好的 blob。校验失败抛 SignatureRejected，并把签名请求标为 rejected。"""
    req = for_approval(approval["id"])
    if req is None or req["status"] != PENDING:
        raise SignatureRejected(f"Approval #{approval['id']} has no pending signing request")
    if time.time() > req["expires_at"]:
        _finish(req["id"], EXPIRED, error="The signing request has expired")
        raise SignatureRejected("The signing request has expired")
    try:
        validate(signed_blob, req, approval)
    except SignatureRejected as exc:
        _finish(req["id"], REJECTED, error=str(exc))
        raise
    _finish(req["id"], SIGNED, signed_blob=signed_blob)
    return get(req["id"])


def cancel(approval_id: int, reason: str) -> None:
    req = for_approval(approval_id)
    if req and req["status"] == PENDING:
        _finish(req["id"], REJECTED, error=reason)


def validate(signed_blob: str, req: dict, approval: dict) -> None:
    try:
        signed = decode(signed_blob)
    except Exception as exc:  # noqa: BLE001 —— 任何解码失败都是不合法的 blob
        raise SignatureRejected(f"Cannot decode the signed transaction: {exc}") from exc
    unsigned = decode(req["unsigned_hex"])
    quote = json.loads(approval["quote_json"])

    if {k: v for k, v in signed.items() if k != "TxnSignature"} != unsigned:
        raise SignatureRejected("The signed transaction differs from the one the backend prepared")
    if signed.get("TransactionType") not in ("Payment", "EscrowCreate") or not isinstance(signed.get("Amount"), str):
        raise SignatureRejected("Only native-XRP Payment / EscrowCreate is accepted")
    if signed.get("Destination") != quote["payTo"]:
        raise SignatureRejected(f"Destination {signed.get('Destination')} does not match the approval's {quote['payTo']}")
    if signed["Amount"] != quote["amount"]:
        raise SignatureRejected(f"Amount {signed['Amount']} drops does not match the approval's {quote['amount']} drops")
    if _terms_hash(quote, signed, approval) != approval["quote_hash"]:
        raise SignatureRejected("The quote_hash computed from the signed transaction does not match the approval")
    if signed.get("Account") != req["signer_account"] or signed.get("SigningPubKey") != req["signing_pub_key"]:
        raise SignatureRejected("The paying account or signing key is not the treasury / device")
    signature = signed.get("TxnSignature")
    if not signature or not is_valid_message(bytes.fromhex(encode_for_signing(unsigned)), bytes.fromhex(signature),
                                             req["signing_pub_key"]):
        raise SignatureRejected("Signature verification failed")


def _terms_hash(quote: dict, signed: dict, approval: dict) -> str:
    """与 Quote.terms_hash() 同一口径：付款条款取自签名交易，验收条件取自审批单快照。"""
    from guard.approvals import acceptance
    from guard.quote import hash_terms
    terms = (quote["scheme"], quote["network"], "XRP", signed["Amount"], signed["Destination"])
    return hash_terms(terms, acceptance(approval))


def _finish(request_id: int, status: str, **fields: Any) -> None:
    cols = "".join(f", {k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE signing_requests SET status = ?{cols} WHERE id = ? AND status = ?",
                     (status, *fields.values(), request_id, PENDING))
