"""把 402 响应解析成 Quote。

格式依据 x402_xrpl 源码（server/fastapi.py 与 types.py）：
- 402 的 JSON body 就是 v2 PaymentRequired：{x402Version, resource, accepts: [...], ...}
- 同一份内容 base64 后放在 PAYMENT-REQUIRED 响应头里
- accepts 里每项是 PaymentRequirements：scheme / network / amount / asset / payTo / extra.invoiceId
解析直接复用 x402_xrpl.types.PaymentRequired.from_dict，不自己猜字段。
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import unicodedata

import requests
from x402_xrpl.types import PaymentRequired, PaymentRequirements, ResourceInfo
from xrpl.utils import drops_to_xrp


class QuoteError(ValueError):
    """402 响应无法解析成合法报价。"""


@dataclass(frozen=True)
class Quote:
    url: str
    requirements: PaymentRequirements
    resource: Optional[ResourceInfo] = None   # 402 里的 resource，组装 PAYMENT-SIGNATURE 时回传
    options: tuple[PaymentRequirements, ...] = ()   # 402 里的全部付款选项（exact / escrow …）

    def with_scheme(self, scheme: str) -> Optional["Quote"]:
        """换成同一份 402 里指定 scheme 的选项；没有就返回 None。"""
        for req in self.options or (self.requirements,):
            if req.scheme == scheme:
                return Quote(self.url, req, self.resource, self.options)
        return None

    @property
    def scheme(self) -> str:
        return self.requirements.scheme

    @property
    def description(self) -> Optional[str]:
        extra = self.requirements.extra or {}
        return extra.get("description") or (self.resource.description if self.resource else None)

    @property
    def network(self) -> str:
        return self.requirements.network

    @property
    def asset(self) -> str:
        return self.requirements.asset

    @property
    def amount(self) -> str:
        return self.requirements.amount

    @property
    def pay_to(self) -> str:
        return self.requirements.pay_to

    @property
    def invoice_id(self) -> str | None:
        return self.requirements.invoice_id()

    @property
    def amount_drops(self) -> int | None:
        """金额的唯一计算口径：整数 drops。仅原生 XRP 有值；IOU 返回 None，由 R1 先拦掉。"""
        if self.asset != "XRP":
            return None
        return int(self.amount)

    @property
    def amount_xrp(self) -> Decimal | None:
        """仅用于展示，不参与任何比较或求和。"""
        if self.asset != "XRP":
            return None
        return drops_to_xrp(self.amount)

    def terms(self) -> tuple[str, str, str, str, str]:
        """付款条款。invoiceId 每次 402 都会变，不参与比对。"""
        r = self.requirements
        return (r.scheme, r.network, r.asset, r.amount, r.pay_to)

    def acceptance(self) -> dict[str, Any]:
        """验收条件：商户对数据的描述、dataSchema 和交付条件。验货只认这些，所以它们也进 quote_hash。"""
        r, extra = self.requirements, self.requirements.extra or {}
        return {
            "description": extra.get("description"),
            "resourceDescription": self.resource.description if self.resource else None,
            "dataSchema": extra.get("dataSchema"),
            "delivery": {"mimeType": self.resource.mime_type if self.resource else None,
                         "maxTimeoutSeconds": r.max_timeout_seconds, "escrow": extra.get("escrow")},
        }

    def terms_hash(self) -> str:
        """付款条款 + 验收条件的规范化 sha256。审批单绑定它：付款前重新拿到的报价哈希不同就拒绝（R8）。"""
        return hash_terms(self.terms(), self.acceptance())

    def snapshot(self) -> dict[str, Any]:
        """审批单保存的完整报价快照；验货只用这份，不用付款时商户再给的描述。"""
        return {"url": self.url, "requirements": self.requirements.to_dict(),
                "resource": self.resource.to_dict() if self.resource else None, "acceptance": self.acceptance()}


def _normalize(value: Any) -> Any:
    """字符串 NFC + 折叠空白；dict 键排序交给 json.dumps。只去掉排版差异，内容的任何变化都会改变哈希。"""
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFC", value).split())
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_terms(terms: tuple | list, acceptance: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json({"payment": list(terms), "acceptance": acceptance}).encode()).hexdigest()


def parse_402(resp: requests.Response) -> Quote:
    payload = _payment_required_payload(resp)
    try:
        required = PaymentRequired.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise QuoteError(f"402 内容不是合法的 PaymentRequired：{exc}") from exc
    if not required.accepts:
        raise QuoteError("402 没有给出任何 accepts 报价")
    req = _select(required.accepts)
    if req.asset == "XRP" and not (req.amount.isascii() and req.amount.isdigit() and int(req.amount) > 0):
        raise QuoteError(f"XRP 金额必须是正整数 drops，收到 {req.amount!r}")
    return Quote(url=resp.url, requirements=req, resource=required.resource, options=tuple(required.accepts))


def _payment_required_payload(resp: requests.Response) -> dict[str, Any]:
    """优先用 body：x402_requests 付款时也是读 body 里的 accepts，判定和付款看的是同一份。"""
    try:
        body = resp.json()
        if isinstance(body, dict) and "accepts" in body:
            return body
    except ValueError:
        pass
    header = resp.headers.get("PAYMENT-REQUIRED")
    if header:
        try:
            return json.loads(base64.b64decode(header))
        except (ValueError, TypeError) as exc:
            raise QuoteError(f"PAYMENT-REQUIRED 头无法解码：{exc}") from exc
    raise QuoteError("402 响应里既没有 accepts body 也没有 PAYMENT-REQUIRED 头")


def _select(accepts: list[PaymentRequirements]) -> PaymentRequirements:
    """和 x402_requests 默认行为一致：优先 scheme=exact，其次服务端给的第一项。"""
    for req in accepts:
        if req.scheme == "exact":
            return req
    return accepts[0]
