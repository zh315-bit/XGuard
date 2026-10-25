"""演示商户 E：先验货再放款（/data/e）。挂在 merchants/demo.py 的同一个 FastAPI 应用上。

这是 AgentGuard 对 x402 的扩展，不是标准 x402：
x402-xrpl 0.3.4 和 testnet facilitator 只支持 exact / upto 两种 scheme，都是“先付款后拿货”。
这里 402 报价使用自定义 scheme "escrow"，买方先在链上用条件式 EscrowCreate 锁款给商户，
再带上“escrow 付款证明”请求头来取货：
    X-ESCROW-OWNER     锁款账户
    X-ESCROW-SEQUENCE  EscrowCreate 的 Sequence
    X-ESCROW-TX        EscrowCreate 的交易哈希
商户在链上核对 escrow（收款人是自己、金额对、带条件、离 CancelAfter 还有足够时间、没被用过）后交付内容。
买方验货通过才用原像 EscrowFinish 放款；不通过就在 CancelAfter 之后 EscrowCancel 退款。

模式开关（演示用）：POST /data/e/mode {"mode": "normal" | "garbage" | "malformed"}
- normal：报价里声明的那一周（上一个完整 ISO 周）7 天的 XRPL DEX 交易量
- garbage：结构完全符合，但内容是 2019 年的样例数据（日期不在报价周内，确定性检查就能拦下）
- malformed：daily 为空

验收周期写在报价的 dataSchema.coverage 里（周次 + 起止日期），随报价进 quote_hash、进审批快照。
用“上一个完整 ISO 周”而不是“最近 7 天”：同一周内（周一到周日）报价都不变，跨天运行不会触发 R8。
"""

import time
import uuid
from datetime import date, timedelta

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from xrpl.models.requests import LedgerEntry
from xrpl.models.requests.ledger_entry import Escrow
from xrpl.utils import ripple_time_to_posix

from guard.config import require_env
from guard.xrpl_client import get_client

PATH = "/data/e"
PRICE_DROPS = 6_000_000            # 6 XRP：超过单笔自动上限（审批）和 escrow 阈值（锁款验货）
# escrow 离 CancelAfter 太近就不交货（来不及放款）。CancelAfter 在设备签名之前就写进了交易，
# 人在设备上核对会吃掉一部分时间，所以演示时（CancelAfter 75 秒）这里不能设得太大
MIN_SECONDS_BEFORE_CANCEL = 15
DESCRIPTION = ("XRPL DEX 周成交量：dataSchema.coverage 声明的那一个 ISO 周（周一至周日）7 天，"
               "每天一个成交量数值（单位 XRP），附 7 天合计")
DATASET = "xrpl_weekly_dex_volume"
DATA_SCHEMA = {
    "profile": "xrpl_dex_7day",          # 验货按这个 profile 做七天 DEX 数据的确定性检查
    "required": ["dataset", "week", "unit", "daily", "total_volume_xrp"],
    "series": {"field": "daily", "min_items": 7, "date_field": "date", "value_field": "volume_xrp"},
    "dataset": DATASET,
    "unit": "XRP",
    "total_field": "total_volume_xrp",
}

state = {"mode": "normal"}


def _today() -> date:
    return date.today()


def coverage(today: date | None = None) -> dict:
    """上一个完整 ISO 周（周一至周日）。"""
    today = today or _today()
    start = today - timedelta(days=today.weekday() + 7)
    year, week, _ = start.isocalendar()
    return {"week": f"{year}-W{week:02d}", "start": start.isoformat(),
            "end": (start + timedelta(days=6)).isoformat(), "days": 7}


def data_schema(today: date | None = None) -> dict:
    return {**DATA_SCHEMA, "coverage": coverage(today)}
_used: set[tuple[str, int]] = set()   # 已经交过货的 escrow，不能重复取货


class ModeBody(BaseModel):
    mode: str


def register(app: FastAPI, network: str) -> None:
    pay_to = require_env("MERCHANT_A_ADDRESS")   # 复用已有链上凭证的 merchant_a 作为收款方

    @app.post(f"{PATH}/mode")
    def set_mode(body: ModeBody) -> dict:
        if body.mode not in ("normal", "garbage", "malformed"):
            raise HTTPException(400, "mode 只能是 normal / garbage / malformed")
        state["mode"] = body.mode
        return {"mode": body.mode}

    @app.get(f"{PATH}/mode")
    def get_mode() -> dict:
        return state

    @app.get(PATH)
    def data_e(request: Request,
               x_escrow_owner: str | None = Header(default=None),
               x_escrow_sequence: int | None = Header(default=None),
               x_escrow_tx: str | None = Header(default=None)):
        if not (x_escrow_owner and x_escrow_sequence is not None and x_escrow_tx):
            return JSONResponse(status_code=402, content=_payment_required(str(request.url), network, pay_to))
        problem = _check_escrow(x_escrow_owner, x_escrow_sequence, pay_to)
        if problem:
            return JSONResponse(status_code=402, content={**_payment_required(str(request.url), network, pay_to),
                                                         "error": f"escrow_invalid: {problem}"})
        _used.add((x_escrow_owner, x_escrow_sequence))
        return _content(state["mode"])


def _payment_required(url: str, network: str, pay_to: str) -> dict:
    return {
        "x402Version": 2,
        "resource": {"url": url, "description": DESCRIPTION, "mimeType": "application/json"},
        "accepts": [{
            "scheme": "escrow", "network": network, "amount": str(PRICE_DROPS), "asset": "XRP", "payTo": pay_to,
            "maxTimeoutSeconds": 600,
            "extra": {
                "invoiceId": uuid.uuid4().hex.upper(),
                "description": DESCRIPTION,
                "dataSchema": data_schema(),
                "escrow": {"condition": "PREIMAGE-SHA-256", "minSecondsBeforeCancel": MIN_SECONDS_BEFORE_CANCEL,
                           "proofHeaders": ["X-ESCROW-OWNER", "X-ESCROW-SEQUENCE", "X-ESCROW-TX"],
                           "note": "AgentGuard escrow extension, not standard x402"},
            },
        }],
        "error": "escrow payment proof required",
        "extensions": {},
    }


def _check_escrow(owner: str, sequence: int, pay_to: str) -> str | None:
    """返回问题描述；没问题返回 None。"""
    if (owner, sequence) in _used:
        return "这个 escrow 已经取过货"
    resp = get_client().request(LedgerEntry(escrow=Escrow(owner=owner, seq=sequence), ledger_index="validated"))
    if not resp.is_successful():
        return f"链上查不到 escrow（{resp.result.get('error')}）"
    node = resp.result["node"]
    if node.get("Destination") != pay_to:
        return "escrow 的收款人不是本商户"
    if node.get("Amount") != str(PRICE_DROPS):
        return f"escrow 金额 {node.get('Amount')} drops 不等于报价 {PRICE_DROPS}"
    if not node.get("Condition"):
        return "escrow 没有条件"
    if "CancelAfter" in node and ripple_time_to_posix(node["CancelAfter"]) - time.time() < MIN_SECONDS_BEFORE_CANCEL:
        return "escrow 离 CancelAfter 太近，来不及放款"
    return None


def _content(mode: str, today: date | None = None) -> dict:
    cov = coverage(today)
    if mode == "malformed":
        return {"dataset": DATASET, "week": cov["week"], "unit": "XRP", "daily": [], "total_volume_xrp": 0}
    if mode == "garbage":
        days = [date(2019, 3, 4) + timedelta(days=i) for i in range(7)]
        return {"dataset": DATASET, "week": cov["week"], "unit": "XRP",
                "note": "placeholder sample data, not real market data",
                "daily": [{"date": d.isoformat(), "volume_xrp": 12345} for d in days], "total_volume_xrp": 12345 * 7}
    start = date.fromisoformat(cov["start"])
    volumes = [2_412_908, 2_655_310, 2_301_774, 2_918_455, 2_726_445, 3_104_962, 2_847_120]
    return {"dataset": DATASET, "week": cov["week"], "unit": "XRP", "source": "XRPL DEX (book_offers + tx history)",
            "daily": [{"date": (start + timedelta(days=i)).isoformat(), "volume_xrp": v} for i, v in enumerate(volumes)],
            "total_volume_xrp": sum(volumes)}
