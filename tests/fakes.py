"""测试用的本地假商户、计数签名器和离线环境。全部不连 XRPL、不真付款。

FakeMerchant 按 x402 v2 的格式回 402；收到 PAYMENT-SIGNATURE 后按 pay_mode：
  ok    → 200 + PAYMENT-RESPONSE（交易哈希就是收到的已签名交易的哈希）
  hang  → 一直不回，模拟“交易已提交但响应丢失”
"""

import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import pytest
import yaml
from x402_xrpl.client.presigned_payment_payer import invoice_id_to_memo_hex
from xrpl.models.transactions import Memo, Payment, Transaction
from xrpl.wallet import Wallet

from guard import credentials, payment_executor, x402_gate
from guard.signers import SoftwareSigner

PAYEE = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"       # “已认证”商户
OTHER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"       # 调包后的地址

BASE_POLICY = {
    "network": "xrpl:1", "allowed_assets": ["XRP"],
    "hard_cap_xrp": 50, "auto_limit_xrp": 1, "daily_budget_xrp": 5, "daily_total_cap_xrp": 20,
    "quote_ttl_seconds": 120, "approval_ttl_seconds": 120, "approval_ttl_seconds_demo": 300,
    "rate_limit": {"window_seconds": 60, "max_requests": 5},
    "blacklist": [],
    "merchant_credential": {"issuer": "rIssuerForTests", "type": "AgentGuardVerifiedMerchant"},
}


def terms(amount_drops: int = 10_000, pay_to: str = PAYEE, network: str = "xrpl:1", asset: str = "XRP") -> dict:
    return {"scheme": "exact", "network": network, "asset": asset, "amount": str(amount_drops),
            "payTo": pay_to, "maxTimeoutSeconds": 600}


class FakeMerchant:
    def __init__(self) -> None:
        self.default_terms = terms()
        self.queue: list[dict] = []          # 依次使用的报价条款；用完后回到 default_terms
        self.pay_mode = "ok"
        self.resource_description = "fake data"
        self.e_mode = "normal"             # /data/e 交付模式：normal / garbage / malformed
        self.escrow_proofs: list[dict] = []
        self.quotes_served = 0
        self.payment_headers: list[str] = []
        self.buyer_headers: list[str | None] = []    # 每个请求带的 X-AgentGuard-Buyer
        self.lock = threading.Lock()

    def next_terms(self) -> dict:
        with self.lock:
            self.quotes_served += 1
            return self.queue.pop(0) if self.queue else self.default_terms


def _handler(merchant: FakeMerchant):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with merchant.lock:
                merchant.buyer_headers.append(self.headers.get("X-AgentGuard-Buyer"))
            if self.path.startswith("/data/e"):
                return self._escrow_merchant()
            if self.path.startswith("/status/"):           # 探测阶段的非 402 响应：/status/404、/status/302 ...
                code = int(self.path.split("/")[2])
                return self._send(code, {"page": "PAGE CONTENT THAT MUST NOT REACH THE AGENT"},
                                  {"Location": "http://elsewhere.example/pay"} if 300 <= code < 400 else None)
            if self.path == "/slow":
                time.sleep(2)
                return self._send(402, {})
            signature = self.headers.get("PAYMENT-SIGNATURE")
            if signature:
                with merchant.lock:
                    merchant.payment_headers.append(signature)
                if merchant.pay_mode == "hang":
                    time.sleep(3)
                    return
                if merchant.pay_mode == "settle_unknown":
                    # 与 x402-xrpl 商户中间件一致：facilitator /settle 超时 → 409，不交货
                    self._send(409, {"error": "settlement_status_unknown", "reason": "settlement_status_unknown",
                                     "retryable": False, "phase": "settle", "settlementAttempted": True})
                    return
                blob = json.loads(base64.b64decode(signature))["payload"]["signedTxBlob"]
                settled = {"success": True, "transaction": Transaction.from_blob(blob).get_hash(), "network": "xrpl:1"}
                self._send(200, {"ok": True, "path": self.path},
                           {"PAYMENT-RESPONSE": base64.b64encode(json.dumps(settled).encode()).decode()})
                return
            accept = {**merchant.next_terms(), "extra": {"invoiceId": uuid.uuid4().hex.upper()}}
            body = {"x402Version": 2, "resource": {"url": f"http://{self.headers['Host']}{self.path}",
                                                    "description": merchant.resource_description},
                    "accepts": [accept], "extensions": {}}
            self._send(402, body)

        def _escrow_merchant(self):
            """复用真实商户 E 的报价与内容；链上核对由测试里的假链负责，这里只记录收到的付款证明。"""
            from merchants import escrow_merchant
            proof = {k: self.headers.get(k) for k in ("X-ESCROW-OWNER", "X-ESCROW-SEQUENCE", "X-ESCROW-TX")}
            if all(proof.values()):
                merchant.escrow_proofs.append(proof)
                return self._send(200, escrow_merchant._content(merchant.e_mode))
            url = f"http://{self.headers['Host']}{self.path}"
            self._send(402, escrow_merchant._payment_required(url, "xrpl:1", PAYEE))

        def _send(self, status, body, headers=None):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass
    return Handler


class CountingSigner(SoftwareSigner):
    """真实签名（离线），同时记录被调用了几次。"""

    def __init__(self) -> None:
        super().__init__(Wallet.create())
        self.calls = 0
        self._lock = threading.Lock()

    async def sign(self, tx):
        with self._lock:
            self.calls += 1
        return await super().sign(tx)


def offline_payment(req, account, signing_pub_key, extra_memos=None) -> Payment:
    """替代 x402_payment.build_payment：同样的字段，只是 Sequence / Fee / LastLedgerSequence 用固定值。"""
    return Payment(
        account=account, destination=req.pay_to, amount=req.amount,
        memos=[Memo(memo_data=invoice_id_to_memo_hex(req.invoice_id())), *(extra_memos or [])],
        sequence=1, fee="10", last_ledger_sequence=1_000, signing_pub_key=signing_pub_key,
    )


def write_policy(tmp_path, monkeypatch, **overrides) -> None:
    policy = {**BASE_POLICY, **overrides}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))
    monkeypatch.setenv("POLICY_PATH", str(path))


@pytest.fixture
def merchant():
    m = FakeMerchant()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(m))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    m.base = f"http://127.0.0.1:{server.server_port}"
    yield m
    server.shutdown()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """离线付款环境：固定策略文件、凭证一律有效、离线构造交易、热钱包换成计数签名器。"""
    write_policy(tmp_path, monkeypatch)
    monkeypatch.setattr(credentials, "is_verified", lambda subject, *a: subject == PAYEE)
    monkeypatch.setattr(payment_executor, "build_payment", offline_payment)
    agent = CountingSigner()
    monkeypatch.setattr(x402_gate, "agent_signer", lambda: agent)
    return agent


def decode_memos(header: str) -> list[dict]:
    blob = json.loads(base64.b64decode(header))["payload"]["signedTxBlob"]
    tx = Transaction.from_blob(blob).to_xrpl()
    out = []
    for m in tx.get("Memos", []):
        out.append({k: bytes.fromhex(v).decode() for k, v in m["Memo"].items()})
    return out


def fetch(merchant: FakeMerchant, path: str = "/data", request_id: Optional[str] = None, max_price=None):
    return x402_gate.fetch_paid(merchant.base + path, "test", max_price, agent_id="test-agent",
                                request_id=request_id or uuid.uuid4().hex)


class FakeChain:
    """替代 guard.chain：记录提交的交易，账本时间可以手动拨动。"""

    def __init__(self) -> None:
        self.time = 800_000_000
        self.submitted: list[dict] = []     # 每项：{"type", "tx", "hash"}
        self.sequence = 100
        self.fail_types: set[str] = set()

    def prepare(self, tx, ledgers=150):
        from dataclasses import replace
        self.sequence += 1
        return replace(tx, sequence=self.sequence, fee="12", last_ledger_sequence=1_000)

    def submit_signed(self, blob):
        tx = Transaction.from_blob(blob)
        return self._record(tx.to_xrpl(), tx.get_hash())

    def submit_with_wallet(self, tx, wallet):
        data = tx.to_xrpl()
        return self._record(data, f"{data['TransactionType'].upper()}{len(self.submitted):04d}".ljust(64, "0"))

    def _record(self, data, tx_hash):
        self.submitted.append({"type": data["TransactionType"], "tx": data, "hash": tx_hash})
        code = "tecNO_PERMISSION" if data["TransactionType"] in self.fail_types else "tesSUCCESS"
        return {"hash": tx_hash, "meta": {"TransactionResult": code}, "tx_json": data}

    def of_type(self, tx_type):
        return [s for s in self.submitted if s["type"] == tx_type]


@pytest.fixture
def fake_chain(monkeypatch):
    from guard import chain
    fake = FakeChain()
    for name in ("prepare", "submit_signed", "submit_with_wallet"):
        monkeypatch.setattr(chain, name, getattr(fake, name))
    monkeypatch.setattr(chain, "ledger_close_time", lambda: fake.time)
    return fake
