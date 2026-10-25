"""demo 重置：解除锁定、处理警报、热钱包恢复到上限（水龙头 + 热钱包签名回收），全程不用金库签名。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient
from xrpl.wallet import Wallet

from guard import api, chain, hot_wallet, lockdown, monitor
from guard.db import connect
from fakes import fake_chain, fetch, merchant, offline  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_sentinel import FakeClient, ledger_msg, run_sentinel, status, tx_msg  # noqa: E402

HOT = Wallet.create()
TREASURY = Wallet.create().address
STRANGER = Wallet.create().address
FAUCET = Wallet.create().address
XRP = 1_000_000
CAP = 20 * XRP
FEE = 12                    # FakeChain.prepare 的手续费


class Ledger:
    """热钱包余额：水龙头到账时增加，热钱包付款提交时扣掉金额 + 手续费。"""

    def __init__(self, fake, balance):
        self.fake, self.balance, self.faucet_calls = fake, balance, []

    def submit_signed(self, blob):
        result = self.fake.submit_signed(blob)
        tx = result["tx_json"]
        if tx["Account"] == HOT.address:
            self.balance -= int(tx["Amount"]) + int(tx["Fee"])
        return result

    def faucet(self, ok=True, amount=100 * XRP):
        def post(url, json=None, timeout=None):
            self.faucet_calls.append(json)
            if not ok:
                raise requests.ConnectionError("faucet unreachable")
            self.balance += amount
            return SimpleNamespace(status_code=200, text="{}")
        return SimpleNamespace(post=post, RequestException=requests.RequestException)


@pytest.fixture
def env(monkeypatch, fake_chain, merchant):  # noqa: F811
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("MERCHANT_BASE_URL", merchant.base)
    monkeypatch.setenv("AGENT_SEED", HOT.seed)
    monkeypatch.setenv("AGENT_ADDRESS", HOT.address)
    monkeypatch.setenv("TREASURY_ADDRESS", TREASURY)

    def no_treasury(role):           # 重置绝不能用金库签名
        assert role == "agent", f"reset tried to load the {role} wallet"
        return HOT
    monkeypatch.setattr(hot_wallet, "load_wallet", no_treasury)
    monkeypatch.setattr(lockdown, "load_wallet", no_treasury)
    monkeypatch.setattr(lockdown, "account_reserve_drops", lambda address: XRP)
    return fake_chain


def ledger(monkeypatch, fake, balance, **faucet):
    led = Ledger(fake, balance)
    monkeypatch.setattr(chain, "submit_signed", led.submit_signed)
    monkeypatch.setattr(chain, "balance_drops", lambda address: led.balance if address == HOT.address else 50 * XRP)
    monkeypatch.setattr(hot_wallet, "requests", led.faucet(**faucet))
    return led


def flag_unauthorized(watched):
    bad = "B" * 64
    run_sentinel([FakeClient([ledger_msg(100), tx_msg(bad, HOT.address, STRANGER, 101)])], watched,
                 until=lambda: status(bad) == "UNAUTHORIZED")
    return bad


def test_reset_after_contain_restores_the_demo(env, monkeypatch):
    watched = {HOT.address: "hot_wallet", TREASURY: "treasury"}
    led = ledger(monkeypatch, env, 19 * XRP)
    bad = flag_unauthorized(watched)
    assert TestClient(api.app).post("/api/lockdown/contain", json={"confirm": True}).status_code == 200
    assert lockdown.is_locked() and led.balance == XRP            # Contain 后只剩储备

    resp = TestClient(api.app).post("/api/demo/reset")
    assert resp.status_code == 200, resp.text
    out = resp.json()

    assert out["lockdown_released"] is True and not lockdown.is_locked()
    assert out["alerts_acknowledged"] == [bad] and monitor.view()["open_alerts"] == []
    assert led.faucet_calls == [{"destination": HOT.address}]
    assert out["hot_wallet"]["ok"] is True and led.balance == CAP and out["hot_wallet"]["balance_xrp"] == "20"

    # 只有热钱包签过名：Contain 的回收 + 重置的回收，都是热钱包 → 金库，没有任何签名请求
    sweeps = env.of_type("Payment")
    assert [(s["tx"]["Account"], s["tx"]["Destination"]) for s in sweeps] == [(HOT.address, TREASURY)] * 2
    refill = sweeps[1]
    assert int(refill["tx"]["Amount"]) == XRP + 100 * XRP - CAP - FEE
    memo = json.loads(bytes.fromhex(refill["tx"]["Memos"][0]["Memo"]["MemoData"]).decode())
    assert memo["action"] == "demo-refill"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM signing_requests").fetchone()[0] == 0
    kinds = [e["kind"] for e in monitor.list_events()]
    assert kinds[:3] == ["demo_refill_sweep", "alert_acknowledged", "lockdown_released"]

    # 哨兵：水龙头转入只记为 incoming，回收交易对账为 matched，都不报警
    faucet_tx, sweep_tx = "F" * 64, refill["hash"]
    client = FakeClient([ledger_msg(200), tx_msg(faucet_tx, FAUCET, HOT.address, 201, drops=str(100 * XRP)),
                         tx_msg(sweep_tx, HOT.address, TREASURY, 202, drops=refill["tx"]["Amount"])])
    _, alerts = run_sentinel([client], watched, until=lambda: status(sweep_tx) is not None and status(faucet_tx) is not None)
    assert alerts == []
    assert status(faucet_tx) == "incoming"
    assert (monitor.get_tx(sweep_tx)["status"], monitor.get_tx(sweep_tx)["match_kind"]) == ("matched", "demo-refill")
    # 处置记录仍是 Contain 那笔，重置的回收不会被当成处置
    assert lockdown.status()["containment"]["tx_hash"] == sweeps[0]["hash"]


def test_faucet_failure_still_completes_the_reset(env, monkeypatch, merchant, offline):  # noqa: F811
    led = ledger(monkeypatch, env, 3 * XRP, ok=False)
    lockdown.lock("test")
    fetch(merchant)

    out = TestClient(api.app).post("/api/demo/reset").json()

    assert out["lockdown_released"] is True and not lockdown.is_locked()
    assert out["hot_wallet"]["ok"] is False
    assert "Hot wallet refill failed, please top up manually" in out["hot_wallet"]["error"]
    assert led.balance == 3 * XRP and env.submitted == []       # 没有发出任何交易
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payment_requests").fetchone()[0] == 0


def test_balance_above_cap_is_swept_without_the_faucet(env, monkeypatch):
    led = ledger(monkeypatch, env, 25 * XRP)
    out = TestClient(api.app).post("/api/demo/reset").json()
    assert led.faucet_calls == [] and out["lockdown_released"] is False
    assert led.balance == CAP and out["hot_wallet"]["sweep"]["amount_xrp"] == "4.999988"


def test_balance_at_cap_does_nothing(env, monkeypatch):
    led = ledger(monkeypatch, env, CAP)
    out = TestClient(api.app).post("/api/demo/reset").json()
    assert out["hot_wallet"]["ok"] is True and led.faucet_calls == [] and env.submitted == []
