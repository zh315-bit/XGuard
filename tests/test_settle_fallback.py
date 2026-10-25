"""演示商户的结算兜底：x402 中间件回 409（facilitator 结算状态未知）时，商户查链确认后才交货。

内层“x402 中间件”是桩：带 PAYMENT-SIGNATURE 的请求一律回 409。交易离线签名，链上查询用假函数。
"""

import asyncio
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from x402_xrpl.client.presigned_payment_payer import invoice_id_to_invoice_id_field
from x402_xrpl.types import PaymentRequirements
from xrpl.models.transactions import Payment
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from merchants import settle_fallback
from merchants.settle_fallback import InvoiceBook, with_chain_fallback

PAY_TO = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
OTHER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
PRICE = "10000"


class Merchant:
    def __init__(self, monkeypatch):
        monkeypatch.setattr(settle_fallback, "CHAIN_WAIT_SECONDS", 0)
        self.book = InvoiceBook()
        self.chain: dict[str, dict] = {}              # tx_hash → 链上结果
        self.handler_calls = 0
        app = FastAPI()

        async def x402_stub(request, call_next):
            if request.headers.get("PAYMENT-SIGNATURE"):
                return JSONResponse(status_code=409, content={"error": "settlement_status_unknown"})
            return await call_next(request)

        app.middleware("http")(with_chain_fallback(x402_stub, self.book, lambda h: self.chain.get(h)))

        @app.get("/data")
        def data():
            self.handler_calls += 1
            return {"ok": True}

        self.client = TestClient(app)

    def issue(self, invoice_id="INV1", amount=PRICE, pay_to=PAY_TO):
        req = PaymentRequirements(scheme="exact", network="xrpl:1", amount=amount, asset="XRP", pay_to=pay_to,
                                  max_timeout_seconds=600, extra={"invoiceId": invoice_id})
        asyncio.run(self.book.put(invoice_id, [req], ttl_seconds=900))

    def pay(self, invoice_id="INV1", destination=PAY_TO, amount=PRICE, invoice_field=None, on_chain=True,
            result="tesSUCCESS", delivered=None):
        wallet = Wallet.create()
        tx = Payment(account=wallet.address, destination=destination, amount=amount, sequence=1, fee="12",
                     last_ledger_sequence=100,
                     invoice_id=invoice_field or invoice_id_to_invoice_id_field(invoice_id))
        signed = sign(tx, wallet)
        if on_chain:
            self.chain[signed.get_hash()] = {"validated": True, "meta": {
                "TransactionResult": result, "delivered_amount": delivered or amount}}
        payload = {"payload": {"signedTxBlob": signed.blob(), "invoiceId": invoice_id}}
        return base64.b64encode(json.dumps(payload).encode()).decode(), signed.get_hash()

    def get(self, header):
        return self.client.get("/data", headers={"PAYMENT-SIGNATURE": header})


@pytest.fixture
def m(monkeypatch):
    return Merchant(monkeypatch)


def test_confirmed_on_chain_delivers(m):
    m.issue()
    header, tx_hash = m.pay()

    resp = m.get(header)

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    settled = json.loads(base64.b64decode(resp.headers["PAYMENT-RESPONSE"]))
    assert settled == {"success": True, "transaction": tx_hash, "network": "xrpl:1", "payer": settled["payer"]}


def test_replay_is_refused(m):
    m.issue()
    header, _ = m.pay()
    assert m.get(header).status_code == 200
    assert m.get(header).status_code == 409          # 同一笔付款不能取两次货
    assert m.handler_calls == 1


@pytest.mark.parametrize("case", ["wrong_destination", "wrong_amount", "unknown_invoice", "invoice_field",
                                  "not_on_chain", "failed_on_chain", "short_delivery"])
def test_mismatch_keeps_409(m, case):
    m.issue()
    kwargs = {
        "wrong_destination": {"destination": OTHER},
        "wrong_amount": {"amount": "9999"},
        "unknown_invoice": {"invoice_id": "NOT-ISSUED"},
        "invoice_field": {"invoice_field": "AB" * 32},
        "not_on_chain": {"on_chain": False},
        "failed_on_chain": {"result": "tecUNFUNDED_PAYMENT"},
        "short_delivery": {"delivered": "1"},
    }[case]
    header, _ = m.pay(**kwargs)

    resp = m.get(header)

    assert resp.status_code == 409 and m.handler_calls == 0


def test_expired_invoice_refused(m, monkeypatch):
    m.issue()
    header, _ = m.pay()
    monkeypatch.setattr(settle_fallback.time, "time", lambda: 10**12)   # invoice 早已过期
    assert m.get(header).status_code == 409


def test_non_409_passes_through(m):
    resp = m.client.get("/data")                     # 没有付款签名：桩直接放行
    assert resp.status_code == 200 and "PAYMENT-RESPONSE" not in resp.headers and m.handler_calls == 1


def test_demo_merchant_routes_use_fallback():
    """演示商户的每个 x402 收费端点都包了兜底，并把签发记录交给同一个 InvoiceBook。"""
    import inspect
    from merchants import demo
    assert "with_chain_fallback" in inspect.getsource(demo.paywall)
    assert "invoice_store=book" in inspect.getsource(demo.paywall)
