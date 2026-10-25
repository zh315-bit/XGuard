"""商户 E 七天 DEX 数据的确定性验货：验收周期只来自批准时冻结的报价快照，不用当前日期推算。"""

import asyncio
from datetime import date

import pytest

from guard import approval_service, delivery_verifier, payments
from guard import state_machine as sm
from merchants import escrow_merchant
from fakes import CountingSigner, fake_chain, fetch, merchant, offline, write_policy  # noqa: F401

QUOTED_ON = date(2026, 10, 24)                     # 报价 / 批准的那天（周六）
SCHEMA = escrow_merchant.data_schema(QUOTED_ON)     # 冻结在审批快照里的 dataSchema：2026-W42


def normal(today=QUOTED_ON):
    return escrow_merchant._content("normal", today)


def failed(delivery, schema=SCHEMA):
    return [c["name"] for c in delivery_verifier.deterministic_checks(schema, delivery) if not c["ok"]]


def with_daily(fn):
    d = normal()
    d["daily"] = fn([dict(i) for i in d["daily"]])
    return d


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    calls = []
    monkeypatch.setattr(delivery_verifier, "_llm_enabled", lambda: True)
    monkeypatch.setattr(delivery_verifier, "_llm_verify", lambda d, x: calls.append(x))   # LLM 不可用
    return calls


def test_coverage_is_previous_full_iso_week():
    assert SCHEMA["coverage"] == {"week": "2026-W42", "start": "2026-10-12", "end": "2026-10-18", "days": 7}
    assert escrow_merchant.coverage(date(2026, 10, 25)) == SCHEMA["coverage"]       # 周日：同一周
    assert escrow_merchant.coverage(date(2026, 10, 26))["week"] == "2026-W43"       # 周一才换周


def test_normal_passes():
    assert failed(normal()) == []
    out = delivery_verifier.verify(escrow_merchant.DESCRIPTION, SCHEMA, normal())
    assert out["verdict"] == "pass" and "semantic check not completed" in out["reason"]


def test_garbage_2019_rejected_without_llm(no_llm):
    garbage = escrow_merchant._content("garbage", QUOTED_ON)
    assert {"date_range", "date_consecutive"} <= set(failed(garbage))
    out = delivery_verifier.verify(escrow_merchant.DESCRIPTION, SCHEMA, garbage)
    assert out["verdict"] == "fail" and out["source"] == "deterministic" and no_llm == []


def test_duplicate_date_rejected():
    dup = with_daily(lambda days: days[:6] + [{**days[6], "date": days[5]["date"]}])
    assert {"date_unique", "date_consecutive"} <= set(failed(dup))


def test_negative_value_rejected():
    neg = with_daily(lambda days: days[:6] + [{**days[6], "volume_xrp": -1}])
    assert "values" in failed(neg)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "100"])
def test_non_finite_or_non_numeric_rejected(value):
    assert "values" in failed(with_daily(lambda days: days[:6] + [{**days[6], "volume_xrp": value}]))


def test_total_mismatch_rejected():
    d = normal()
    d["total_volume_xrp"] += 1
    assert failed(d) == ["total"]


@pytest.mark.parametrize("change, check", [
    ({"unit": "USD"}, "unit"), ({"week": "2026-W43"}, "week"), ({"dataset": "sample_numbers_v0"}, "dataset")])
def test_must_match_quote(change, check):
    assert check in failed({**normal(), **change})


def test_merchant_cannot_widen_range_in_delivery():
    """交付内容里自己写一个别的周次 / 区间没用：基准只看快照里的 coverage。"""
    other_week = escrow_merchant._content("normal", date(2026, 10, 27))
    assert {"week", "date_range"} <= set(failed(other_week))
    assert "coverage" in failed(normal(), {**SCHEMA, "coverage": None})              # 快照里没有周期 → 不放款
    assert "coverage" in failed(normal(), {**SCHEMA, "coverage": {**SCHEMA["coverage"], "end": "2026-10-25"}})


def test_verifier_ignores_system_date(monkeypatch):
    class FarFuture(date):
        @classmethod
        def today(cls):
            return date(2030, 1, 1)
    monkeypatch.setattr(delivery_verifier, "date", FarFuture)
    assert failed(normal()) == []


# ---- 端到端：10 月 24 日报价、批准，10 月 25 日才锁款取货 ----

@pytest.fixture
def escrow_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    write_policy(tmp_path, monkeypatch, escrow_threshold_xrp=5, escrow_cancel_after_seconds=600,
                 hot_wallet_max_balance_xrp=20)


@pytest.mark.parametrize("mode, released", [("normal", True), ("garbage", False)])
def test_quoted_oct24_delivered_oct25(escrow_policy, merchant, offline, fake_chain, monkeypatch, mode, released):
    merchant.e_mode = mode
    monkeypatch.setattr(escrow_merchant, "_today", lambda: QUOTED_ON)
    r = fetch(merchant, "/data/e")
    assert r.state == sm.PENDING_APPROVAL

    monkeypatch.setattr(escrow_merchant, "_today", lambda: date(2026, 10, 25))
    view = asyncio.run(approval_service.approve(r.approval_id, CountingSigner()))

    assert payments.get(r.payment_id)["rule_id"] != "R8"                            # 同一周，报价没变
    assert (view["payment_state"] == sm.RESOURCE_DELIVERED) is released
    assert (len(fake_chain.of_type("EscrowFinish")) == 1) is released
    assert view["escrow"]["verification"]["verdict"] == ("pass" if released else "fail")
