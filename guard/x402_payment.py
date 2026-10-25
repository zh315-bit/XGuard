"""用任意 Signer 完成 x402 exact 付款（审批付款走这里，热钱包 AUTO 付款仍用 x402_requests）。

为什么需要：x402_xrpl 的 XRPLPresignedPaymentPayer.prepare_payment 内部直接 sign(filled, wallet)，
只能传 Wallet，Ledger 这类外部签名器接不进去。但包里公开了
build_payment_header_for_signed_blob(req, signed_tx_blob, invoice_id, ...)，可以接收外部签好的 blob。

所以这里按 prepare_payment 的规则（presigned_payment_payer.py）自己构造 Payment：
- invoiceId 同时写进 Memo（MemoData=hex(invoiceId)）和 InvoiceID 字段（sha256），即 binding="both"
- extra.sourceTag → SourceTag，extra.destinationTag → DestinationTag
- LastLedgerSequence = 最新已验证账本 + ceil(maxTimeoutSeconds / 5) + 2
- 校验 RPC 的 network_id 与报价网络一致，手续费不超过上限
再追加守卫自己的审计 Memo，交给 Signer 签名，最后用包里的函数组装 PAYMENT-SIGNATURE。
只支持原生 XRP（R1 只放行 XRP）。
"""

import json
import math
from typing import Any, Mapping, Optional

from x402_xrpl.client.presigned_payment_payer import (
    build_payment_header_for_signed_blob,
    invoice_id_to_invoice_id_field,
    invoice_id_to_memo_hex,
    text_to_memo_hex,
)
from x402_xrpl.types import PaymentRequirements, ResourceInfo
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import ServerInfo
from xrpl.models.transactions import Memo, Payment
from xrpl.transaction import autofill

from guard.xrpl_client import get_client

DECISION_MEMO_TYPE = "agentguard/decision"
MAX_FEE_DROPS = 10_000


def decision_memo(payload: Mapping[str, Any], memo_type: str = DECISION_MEMO_TYPE) -> Memo:
    return Memo(
        memo_type=text_to_memo_hex(memo_type),
        memo_data=text_to_memo_hex(json.dumps(payload, separators=(",", ":"), ensure_ascii=False)),
        memo_format=text_to_memo_hex("application/json"),
    )


def build_payment(req: PaymentRequirements, account: str, signing_pub_key: str,
                  extra_memos: Optional[list[Memo]] = None) -> Payment:
    """构造并 autofill 一笔待签的 x402 Payment。"""
    _check_supported(req)
    invoice_id = req.invoice_id()
    extra = req.extra or {}
    client = get_client()
    last_ledger = get_latest_validated_ledger_sequence(client) + math.ceil(req.max_timeout_seconds / 5) + 2

    tx = Payment(
        account=account,
        destination=req.pay_to,
        amount=str(int(req.amount)),
        memos=[Memo(memo_data=invoice_id_to_memo_hex(invoice_id)), *(extra_memos or [])],
        invoice_id=invoice_id_to_invoice_id_field(invoice_id),
        source_tag=_opt_int(extra.get("sourceTag")),
        destination_tag=_opt_int(extra.get("destinationTag")),
        last_ledger_sequence=last_ledger,
        signing_pub_key=signing_pub_key,
    )
    filled = autofill(tx, client)
    _check_network_and_fee(req, filled)
    return filled


def payment_header(req: PaymentRequirements, signed_blob: str, resource: Optional[ResourceInfo] = None) -> str:
    return build_payment_header_for_signed_blob(
        req=req, signed_tx_blob=signed_blob, invoice_id=req.invoice_id(), resource=resource,
    )


def _check_supported(req: PaymentRequirements) -> None:
    if req.scheme != "exact" or req.asset != "XRP":
        raise ValueError(f"Only exact + XRP is supported, got {req.scheme} / {req.asset}")
    if not req.invoice_id():
        raise ValueError("The quote is missing extra.invoiceId")
    if (req.extra or {}).get("facilitator"):
        # prepare_payment 此时会追加一个 facilitator memo；演示商户不用，先不实现
        raise ValueError("Quotes with extra.facilitator are not supported yet")


def _check_network_and_fee(req: PaymentRequirements, filled: Payment) -> None:
    info = get_client().request(ServerInfo()).result.get("info", {})
    expected = int(req.network.split(":")[1])
    if info.get("network_id") != expected:
        raise ValueError(f"RPC node network {info.get('network_id')} does not match the quote network {req.network}")
    if not 1 <= int(filled.fee) <= MAX_FEE_DROPS:
        raise ValueError(f"Fee {filled.fee} drops exceeds the limit of {MAX_FEE_DROPS}")


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)
