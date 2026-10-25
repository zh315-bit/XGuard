"""独立哨兵（watchdog/sentinel.py）：已授权交易 → matched，绕过守卫的交易 → UNAUTHORIZED，断线重连后补查。

全部离线：假的 websocket 客户端按脚本推送订阅消息、按账本回 account_tx；已授权的付款走守卫真实的 x402 流程（假商户）。
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest
from xrpl.models.requests import AccountTx, Subscribe
from xrpl.wallet import Wallet

from guard import monitor
from guard.db import connect
from fakes import PAYEE, fetch, merchant, offline  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "watchdog"))
import sentinel as sentinel_mod  # noqa: E402

TREASURY = Wallet.create().address
STRANGER = Wallet.create().address


class FakeResponse:
    def __init__(self, result: dict, ok: bool = True) -> None:
        self.result, self.ok = result, ok

    def is_successful(self) -> bool:
        return self.ok


class FakeClient:
    """messages 推完后：close=True 断开（模拟断线），否则一直保持连接。history：地址 → account_tx 条目。"""

    def __init__(self, messages=(), history=None, validated=100, close=False) -> None:
        self.messages, self.history, self.validated, self.close = list(messages), history or {}, validated, close
        self.requests: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, req):
        self.requests.append(req)
        if isinstance(req, Subscribe):
            return FakeResponse({"ledger_index": self.validated})
        if isinstance(req, AccountTx):
            return FakeResponse({"transactions": [t for t in self.history.get(req.account, [])
                                                  if t["ledger_index"] >= req.ledger_index_min]})
        raise AssertionError(f"unexpected request {req}")

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for msg in self.messages:
            await asyncio.sleep(0.01)
            yield msg
        if not self.close:
            await asyncio.Event().wait()


def tx_msg(tx_hash, account, destination, ledger, drops="10000", tx_type="Payment", **fields):
    """API v2 格式的已验证交易（订阅推送和 account_tx 条目共用）。"""
    return {"type": "transaction", "validated": True, "hash": tx_hash, "ledger_index": ledger,
            "close_time_iso": "2026-10-24T12:00:00Z",
            "tx_json": {"Account": account, "Destination": destination, "TransactionType": tx_type,
                        "DeliverMax": drops, **fields},
            "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": drops}}


def ledger_msg(index):
    return {"type": "ledgerClosed", "ledger_index": index}


def run_sentinel(clients, watched, until, grace=0.0, timeout=5.0):
    """依次用 clients 建连接（用完后给一个空的、不断开的连接），直到 until() 为真。返回 (sentinel, 告警列表)。"""
    alerts: list[dict] = []
    pool = list(clients)

    def connect(_url):
        return pool.pop(0) if pool else FakeClient()

    s = sentinel_mod.Sentinel(url="wss://fake.quiknode.pro/token/", connect=connect, watched=watched,
                              grace_seconds=grace, tick_seconds=0.02, retry_base=0.01, alert_hook=alerts.append)

    async def main():
        task = asyncio.create_task(s.run())
        deadline = time.time() + timeout
        while not until():
            assert time.time() < deadline, "sentinel did not reach the expected state"
            assert not task.done(), task.exception() if task.done() else None
            await asyncio.sleep(0.02)
        s.stop()
        await task

    asyncio.run(main())
    return s, alerts


@pytest.fixture
def accounts(monkeypatch, offline):  # noqa: F811
    """热钱包 = 离线付款用的签名器；金库随便一个地址。"""
    monkeypatch.setenv("AGENT_ADDRESS", offline.address)
    monkeypatch.setenv("TREASURY_ADDRESS", TREASURY)
    return {offline.address: "hot_wallet", TREASURY: "treasury"}


def status(tx_hash):
    row = monitor.get_tx(tx_hash)
    return row and row["status"]


def test_authorized_payment_is_matched(merchant, offline, accounts):  # noqa: F811
    r = fetch(merchant, "/data")
    assert r.verdict == "AUTO"
    with connect() as conn:
        tx_hash = conn.execute("SELECT tx_hash FROM payment_requests").fetchone()["tx_hash"]

    client = FakeClient([ledger_msg(100), tx_msg(tx_hash, offline.address, PAYEE, 101)])
    _, alerts = run_sentinel([client], accounts, until=lambda: status(tx_hash) is not None)

    row = monitor.get_tx(tx_hash)
    assert (row["status"], row["match_kind"], row["direction"], row["source"]) == ("matched", "x402", "outgoing", "stream")
    assert row["amount"] == "0.01 XRP" and row["watched"] == "hot_wallet"
    assert alerts == [] and monitor.list_events() == []
    assert monitor.view()["counts"]["matched"] == 1


def test_bypassing_the_guard_is_unauthorized(accounts, offline):
    bad = "B" * 64
    client = FakeClient([ledger_msg(100), tx_msg(bad, offline.address, STRANGER, 101)])
    _, alerts = run_sentinel([client], accounts, until=lambda: status(bad) == "UNAUTHORIZED")

    assert [a["hash"] for a in alerts] == [bad]
    event, = monitor.list_events()
    assert (event["kind"], event["tx_hash"]) == ("unauthorized_tx", bad)
    view = monitor.view()
    assert view["counts"]["unauthorized"] == 1
    assert [a["hash"] for a in view["open_alerts"]] == [bad]
    assert view["open_alerts"][0]["explorer_url"].endswith(bad)

    # 标记为已处理：警报消失，交易仍是 UNAUTHORIZED，审计里多一条确认事件
    monitor.acknowledge(bad, "rotated the hot wallet key")
    view = monitor.view()
    assert view["open_alerts"] == [] and view["counts"]["unauthorized"] == 1
    assert [e["kind"] for e in monitor.list_events()] == ["alert_acknowledged", "unauthorized_tx"]


def test_incoming_is_only_recorded(accounts):
    tx_hash = "C" * 64
    client = FakeClient([tx_msg(tx_hash, STRANGER, TREASURY, 101)])
    _, alerts = run_sentinel([client], accounts, until=lambda: status(tx_hash) is not None)
    row = monitor.get_tx(tx_hash)
    assert (row["status"], row["direction"], row["watched"]) == ("incoming", "incoming", "treasury")
    assert alerts == []


def test_escrow_release_matched_before_hash_is_written(accounts, offline):
    """EscrowFinish 的哈希是提交返回后才写库的：按 Owner + OfferSequence 认出是守卫建的 escrow。"""
    with connect() as conn:
        conn.execute("INSERT INTO escrows (payment_request_id, created_at, owner, destination, amount_drops, condition, "
                     "preimage, cancel_after, offer_sequence, status) VALUES (7, 0, ?, ?, 1, 'c', 'p', 0, 42, 'releasing')",
                     (TREASURY, PAYEE))
    finish = "D" * 64
    client = FakeClient([tx_msg(finish, offline.address, None, 101, tx_type="EscrowFinish", Owner=TREASURY, OfferSequence=42)])
    run_sentinel([client], accounts, until=lambda: status(finish) is not None)
    row = monitor.get_tx(finish)
    assert (row["status"], row["match_kind"], row["match_ref"]) == ("matched", "escrow-release", "payment #7")
    assert row["watched"] == "hot_wallet,treasury"


def test_record_written_during_grace_period_is_matched(accounts, offline):
    """守卫刚提交、还没写库时哨兵先看到交易：宽限期内是 pending，写库后对上 → matched，不误报。"""
    tx_hash = "E" * 64
    client = FakeClient([tx_msg(tx_hash, offline.address, PAYEE, 101)])

    def until():
        if status(tx_hash) == "pending":
            with connect() as conn:     # 守卫这时才把哈希写进审计
                conn.execute("INSERT INTO decisions (ts, created_at, url, verdict, rule_id, reason, status, tx_hash) "
                             "VALUES (0, '', 'http://x', 'AUTO', 'R0', 'ok', 'paid', ?)", (tx_hash,))
        return status(tx_hash) == "matched"

    _, alerts = run_sentinel([client], accounts, until=until, grace=0.3)
    assert alerts == [] and monitor.get_tx(tx_hash)["match_kind"] == "audit"


def test_reconnect_backfills_transactions_missed_while_disconnected(merchant, offline, accounts):  # noqa: F811
    fetch(merchant, "/data")
    with connect() as conn:
        paid = conn.execute("SELECT tx_hash FROM payment_requests").fetchone()["tx_hash"]
    missed = "F" * 64

    # 第一次连接：看到账本 100、101 后断线
    first = FakeClient([ledger_msg(100), ledger_msg(101)], validated=100, close=True)
    # 断线期间（账本 102、103）发生了一笔已授权付款和一笔绕过守卫的转账，只能靠 account_tx 补查
    history = {offline.address: [tx_msg(paid, offline.address, PAYEE, 102), tx_msg(missed, offline.address, STRANGER, 103)]}
    second = FakeClient(history=history, validated=104)

    s, alerts = run_sentinel([first, second], accounts, until=lambda: status(missed) == "UNAUTHORIZED")

    backfills = [r for r in second.requests if isinstance(r, AccountTx)]
    assert {r.account for r in backfills} == set(accounts)
    assert all(r.ledger_index_min == 101 and r.forward for r in backfills)     # 从断线前处理到的账本补查
    assert monitor.get_tx(missed)["source"] == "backfill"
    assert (monitor.get_tx(paid)["status"], monitor.get_tx(paid)["source"]) == ("matched", "backfill")
    assert [a["hash"] for a in alerts] == [missed]
    assert s.reconnects == 1
    health = monitor.health()
    assert health["status"] == "stopped" and health["reconnects"] == 1
    assert health["last_backfill"]["found"] == 2 and health["last_backfill"]["from_ledger"] == 101
    assert health["endpoint"] == "fake.quiknode.pro (Quicknode)"      # token 不落库


def test_stream_and_backfill_overlap_is_deduplicated(accounts, offline):
    bad = "A" * 64
    msg = tx_msg(bad, offline.address, STRANGER, 101)
    first = FakeClient([ledger_msg(100), msg], close=True)
    second = FakeClient(history={offline.address: [msg]}, validated=102)
    _, alerts = run_sentinel([first, second], accounts,
                             until=lambda: any(isinstance(r, AccountTx) for r in second.requests) and status(bad) == "UNAUTHORIZED")
    assert len(alerts) == 1 and len(monitor.list_events()) == 1


def test_normalize_api_v1_message():
    msg = {"type": "transaction", "validated": True, "ledger_index": 5,
           "transaction": {"hash": "H" * 64, "Account": "rA", "Destination": "rB", "TransactionType": "Payment",
                           "Amount": {"currency": "USD", "issuer": "rI", "value": "3"}, "date": 0},
           "meta": {"TransactionResult": "tecPATH_DRY"}}
    tx = sentinel_mod.normalize(msg)
    assert (tx["hash"], tx["amount"], tx["result"], tx["close_time"]) == ("H" * 64, "3 USD", "tecPATH_DRY", 946684800.0)
    assert sentinel_mod.normalize({**msg, "validated": False}) is None


def test_health_reports_stale_heartbeat():
    monitor.set_state(status="connected", heartbeat=time.time() - 100)
    assert monitor.health()["status"] == "stale"
    monitor.set_state(heartbeat=time.time())
    assert monitor.health()["status"] == "connected"


def test_api_monitor_alert_and_ack(accounts, offline):
    from fastapi.testclient import TestClient
    from guard.api import app
    bad = "9" * 64
    run_sentinel([FakeClient([ledger_msg(100), tx_msg(bad, offline.address, STRANGER, 101)])], accounts,
                 until=lambda: status(bad) == "UNAUTHORIZED")
    api = TestClient(app)
    assert api.get("/health").json()["sentinel"] == "stopped"
    assert [a["hash"] for a in api.get("/api/monitor").json()["open_alerts"]] == [bad]
    assert api.post(f"/api/monitor/alerts/{bad}/ack", json={"note": "checked"}).json()["ack_note"] == "checked"
    assert api.get("/api/monitor").json()["open_alerts"] == []
    assert api.post(f"/api/monitor/alerts/{'0' * 64}/ack").status_code == 404
