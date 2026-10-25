"""安全锁定与 Contain：锁定后付款被拒、待审批不能批准、回收交易被哨兵对账为 matched、解除后恢复、不点 Contain 什么都不发生。"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from xrpl.models.transactions import Transaction
from xrpl.wallet import Wallet

from guard import api, approval_service, chain, lockdown, monitor
from guard.approval_service import ApprovalError
from guard.db import connect
from fakes import CountingSigner, fake_chain, fetch, merchant, offline, terms  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_sentinel import FakeClient, ledger_msg, run_sentinel, status, tx_msg  # noqa: E402

HOT = Wallet.create()
TREASURY = Wallet.create().address
STRANGER = Wallet.create().address
RESERVE = 1_000_000          # 1 XRP
BALANCE = 19_500_000         # 19.5 XRP


@pytest.fixture
def wallets(monkeypatch, fake_chain):  # noqa: F811
    monkeypatch.setenv("AGENT_SEED", HOT.seed)
    monkeypatch.setenv("AGENT_ADDRESS", HOT.address)
    monkeypatch.setenv("TREASURY_ADDRESS", TREASURY)
    monkeypatch.setattr(chain, "balance_drops", lambda address: BALANCE)
    monkeypatch.setattr(lockdown, "account_reserve_drops", lambda address: RESERVE)
    return {HOT.address: "hot_wallet", TREASURY: "treasury"}


def events():
    return [e["kind"] for e in monitor.list_events()]


def test_locked_guard_denies_every_payment(merchant, offline):  # noqa: F811
    lockdown.lock("test")
    r = fetch(merchant, "/data")
    assert (r.verdict, r.rule_id, r.paid) == ("DENY", "LOCKDOWN", False)
    assert r.reason == lockdown.REASON
    assert offline.calls == 0 and merchant.payment_headers == [] and merchant.quotes_served == 0


def test_lockdown_survives_restart(merchant, offline):  # noqa: F811
    lockdown.lock("test")
    from guard import db
    db._initialized.clear()                      # 模拟进程重启：重新打开数据库
    assert lockdown.is_locked() and lockdown.status()["locked"] is True


def test_pending_approval_cannot_be_approved_while_locked(merchant, offline):  # noqa: F811
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant, "/data/b", max_price=100)
    assert (r.verdict, r.rule_id) == ("HOLD", "R4")
    lockdown.lock("test")
    treasury = CountingSigner()
    with pytest.raises(ApprovalError) as exc:
        asyncio.run(approval_service.approve(r.approval_id, treasury))
    assert exc.value.status_code == 423 and treasury.calls == 0 and merchant.payment_headers == []


def test_contain_sweeps_hot_wallet_and_sentinel_matches_it(wallets, fake_chain):  # noqa: F811
    resp = TestClient(api.app).post("/api/lockdown/contain", json={"confirm": True})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["locked"] is True

    sweep, = fake_chain.of_type("Payment")
    tx = sweep["tx"]
    fee = 12
    assert (tx["Account"], tx["Destination"]) == (HOT.address, TREASURY)
    assert int(tx["Amount"]) == BALANCE - RESERVE - fee            # 保留储备和手续费
    memo = json.loads(bytes.fromhex(tx["Memos"][0]["Memo"]["MemoData"]).decode())
    assert memo["action"] == "contain"
    assert out["swept"]["tx_hash"] == sweep["hash"] == Transaction.from_xrpl(tx).get_hash()
    assert out["containment"]["tx_hash"] == sweep["hash"] and out["containment"]["amount_xrp"] == "18.499988"
    assert events() == ["contain_sweep", "lockdown_engaged"]

    client = FakeClient([ledger_msg(100), tx_msg(sweep["hash"], HOT.address, TREASURY, 101, drops=tx["Amount"])])
    _, alerts = run_sentinel([client], wallets, until=lambda: status(sweep["hash"]) is not None)
    row = monitor.get_tx(sweep["hash"])
    assert (row["status"], row["match_kind"]) == ("matched", "contain") and alerts == []


def test_contain_requires_confirmation(wallets, fake_chain):  # noqa: F811
    resp = TestClient(api.app).post("/api/lockdown/contain", json={"confirm": False})
    assert resp.status_code == 400
    assert not lockdown.is_locked() and fake_chain.submitted == []


def test_release_restores_payments_and_is_audited(merchant, offline):  # noqa: F811
    lockdown.lock("test")
    assert fetch(merchant, "/data").rule_id == "LOCKDOWN"
    resp = TestClient(api.app).post("/api/lockdown/release", json={"note": "incident closed"})
    assert resp.status_code == 200 and resp.json()["locked"] is False
    assert events()[0] == "lockdown_released"
    assert json.loads(monitor.list_events()[0]["detail"])["note"] == "incident closed"
    r = fetch(merchant, "/data")
    assert (r.verdict, r.paid) == ("AUTO", True)
    assert TestClient(api.app).post("/api/lockdown/release").status_code == 409


def test_without_contain_an_alert_changes_nothing(wallets, fake_chain, merchant, offline):  # noqa: F811
    bad = "B" * 64
    client = FakeClient([ledger_msg(100), tx_msg(bad, HOT.address, STRANGER, 101)])
    _, alerts = run_sentinel([client], wallets, until=lambda: status(bad) == "UNAUTHORIZED")
    assert [a["hash"] for a in alerts] == [bad]
    # 警报出现了，但没人点 Contain：不锁定、不发任何交易、付款照常
    assert not lockdown.is_locked() and fake_chain.submitted == []
    assert events() == ["unauthorized_tx"]
    assert fetch(merchant, "/data").verdict == "AUTO"


def test_contain_is_only_reachable_from_the_console_api():
    root = Path(__file__).resolve().parent.parent
    callers = [p.relative_to(root).as_posix() for d in ("guard", "watchdog", "agent", "scripts", "merchants")
               for p in (root / d).rglob("*.py") if "lockdown.contain(" in p.read_text()]
    assert callers == ["guard/api.py"]
