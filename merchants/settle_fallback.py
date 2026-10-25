"""演示商户的结算兜底：facilitator 结算状态未知（x402 中间件回 409）时，商户自己查链确认后交货。

背景：T54 testnet facilitator 的 /settle 有时把交易提交上链后不返回结果，x402-xrpl 商户中间件
等到超时就回 409 settlement_status_unknown，不交货——买方的钱已经付了，却拿不到数据。

兜底只在“中间件回 409 且请求带了 PAYMENT-SIGNATURE”时介入，交货前必须全部满足：
- InvoiceID：付款证明里的 invoiceId 是本商户签发、仍在有效期内、没被用过（InvoiceBook）
- 交易本身：是 Payment，Destination = 报价的收款方，Amount = 报价金额（drops），
  InvoiceID 字段 = sha256(invoiceId)
- 链上：按交易哈希查到已验证且 tesSUCCESS，实际到账金额 = 报价金额
- 防重放：同一笔交易、同一个 invoiceId 只交一次货
然后执行原本的资源处理函数，并补上 PAYMENT-RESPONSE 头。这是商户侧的增强，不改变 x402 协议本身。
"""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

from starlette.requests import Request
from starlette.responses import Response
from x402_xrpl.client.presigned_payment_payer import invoice_id_to_invoice_id_field
from x402_xrpl.types import PaymentRequirements
from xrpl.core.binarycodec import decode
from xrpl.models.requests import Tx
from xrpl.models.transactions import Transaction

log = logging.getLogger("merchant.settle_fallback")

CHAIN_WAIT_SECONDS = 20     # 交易可能还在等验证：最多轮询这么久
Middleware = Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]
Lookup = Callable[[str], Optional[dict]]


class InvoiceBook:
    """x402 中间件的 InvoiceStore 实现（内存）。兜底逻辑也从这里核对 invoice 是不是本商户签发的。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, list[PaymentRequirements]]] = {}

    async def put(self, invoice_id: str, reqs: Sequence[PaymentRequirements], *, ttl_seconds: int) -> None:
        self._data[invoice_id] = (time.time() + ttl_seconds, list(reqs))

    async def get(self, invoice_id: str) -> Optional[Sequence[PaymentRequirements]]:
        entry = self._data.get(invoice_id)
        if entry is None or entry[0] < time.time():
            return None
        return entry[1]

    async def consume(self, invoice_id: str) -> None:
        self._data.pop(invoice_id, None)


def lookup_validated(tx_hash: str) -> Optional[dict]:
    """查链：已验证的交易返回 result，查不到 / 未验证返回 None。"""
    from guard.xrpl_client import get_client
    resp = get_client().request(Tx(transaction=tx_hash))
    if not resp.is_successful() or not resp.result.get("validated"):
        return None
    return resp.result


def with_chain_fallback(inner: Middleware, book: InvoiceBook, lookup: Lookup = lookup_validated) -> Middleware:
    used_tx: set[str] = set()

    async def middleware(request: Request, call_next):
        response = await inner(request, call_next)
        signature = request.headers.get("PAYMENT-SIGNATURE")
        if response.status_code != 409 or not signature:
            return response
        try:
            proof = await verify_on_chain(signature, book, used_tx, lookup)
        except Exception as exc:  # noqa: BLE001 —— 兜底失败就维持原来的 409
            log.warning("结算兜底未通过：%s", exc)
            return response
        await book.consume(proof["invoice_id"])
        used_tx.add(proof["tx_hash"])
        log.warning("facilitator 结算状态未知，已按链上交易 %s 确认收款并交货", proof["tx_hash"])
        delivered = await call_next(request)
        settled = {"success": True, "transaction": proof["tx_hash"], "network": proof["network"], "payer": proof["payer"]}
        delivered.headers["PAYMENT-RESPONSE"] = base64.b64encode(json.dumps(settled).encode()).decode()
        return delivered

    return middleware


async def verify_on_chain(signature: str, book: InvoiceBook, used_tx: set[str], lookup: Lookup) -> dict[str, Any]:
    payload = json.loads(base64.b64decode(signature))
    blob = payload["payload"]["signedTxBlob"]
    invoice_id = payload["payload"]["invoiceId"]
    tx_hash = Transaction.from_blob(blob).get_hash()
    tx = decode(blob)

    if tx_hash in used_tx:
        raise ValueError(f"交易 {tx_hash} 已经交过货（重放）")
    reqs = await book.get(invoice_id)
    if not reqs:
        raise ValueError(f"invoiceId {invoice_id} 不是本商户签发的、已过期或已使用")
    req = next((r for r in reqs if r.pay_to == tx.get("Destination") and r.amount == tx.get("Amount")), None)
    if tx.get("TransactionType") != "Payment" or req is None:
        raise ValueError("交易的收款方或金额与报价不符")
    if tx.get("InvoiceID") != invoice_id_to_invoice_id_field(invoice_id):
        raise ValueError("交易的 InvoiceID 与报价的 invoiceId 不符")

    result = await _wait_validated(tx_hash, lookup)
    if result is None:
        raise ValueError(f"交易 {tx_hash} 在 {CHAIN_WAIT_SECONDS} 秒内没有在链上验证")
    meta = result.get("meta", {})
    if meta.get("TransactionResult") != "tesSUCCESS" or meta.get("delivered_amount") != req.amount:
        raise ValueError(f"链上结果 {meta.get('TransactionResult')}，到账 {meta.get('delivered_amount')} 与报价不符")
    return {"tx_hash": tx_hash, "invoice_id": invoice_id, "network": req.network, "payer": tx.get("Account")}


async def _wait_validated(tx_hash: str, lookup: Lookup) -> Optional[dict]:
    deadline = time.time() + CHAIN_WAIT_SECONDS
    while True:
        result = await asyncio.to_thread(lookup, tx_hash)
        if result is not None or time.time() > deadline:
            return result
        await asyncio.sleep(2)
