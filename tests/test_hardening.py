"""状态机、R6 统计口径、demo reset、整数 drops。"""

import pytest
from fastapi.testclient import TestClient

from guard import api, hot_wallet, payments, policy
from guard import state_machine as sm
from guard.db import connect
from fakes import OTHER, PAYEE, fetch, merchant, offline, terms, write_policy  # noqa: F401


# ---- 状态机 ----

def _new_request() -> int:
    row, _ = payments.create_or_get("sm-test", "r1", "http://x", "test")
    return row["id"]


def test_legal_path_walks_to_delivered():
    pid = _new_request()
    for state in (sm.QUOTED, sm.EVALUATED, sm.RESERVED, sm.SIGNING, sm.SUBMITTED, sm.VALIDATED, sm.RESOURCE_DELIVERED):
        sm.advance(pid, state)
    assert payments.get(pid)["state"] == sm.RESOURCE_DELIVERED


@pytest.mark.parametrize("path", [
    [sm.SUBMITTED],                                             # 跳过判定和预留直接提交
    [sm.QUOTED, sm.EVALUATED, sm.DENIED, sm.RESERVED],          # 终态不能复活
    [sm.QUOTED, sm.EVALUATED, sm.PENDING_APPROVAL, sm.SIGNING], # 审批通过前不能签名
    [sm.QUOTED, sm.EVALUATED, sm.RESERVED, sm.SIGNING, sm.SUBMITTED, sm.FAILED],  # 已提交不能直接判失败
    [sm.QUOTED, sm.EVALUATED, sm.RESERVED, sm.SIGNING, sm.SUBMITTED, sm.SIGNING],  # 已提交不能重新签名
])
def test_illegal_transition_raises(path):
    pid = _new_request()
    *ok, bad = path
    for state in ok:
        sm.advance(pid, state)
    with pytest.raises(sm.IllegalTransition):
        sm.advance(pid, bad)
    assert payments.get(pid)["state"] == (ok[-1] if ok else sm.RECEIVED)


def test_transition_is_compare_and_set():
    """同一转移只能成功一次：第二个调用者看到的已经不是合法前驱。"""
    pid = _new_request()
    sm.advance(pid, sm.QUOTED)
    with pytest.raises(sm.IllegalTransition):
        sm.advance(pid, sm.QUOTED)


def test_state_fields_cannot_be_smuggled():
    pid = _new_request()
    with pytest.raises(ValueError):
        sm.advance(pid, sm.QUOTED, state="RESOURCE_DELIVERED")


# ---- R6 只统计真正发起过付款的请求 ----

def test_R6_ignores_denied_requests(merchant, offline, tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch, rate_limit={"window_seconds": 60, "max_requests": 2})
    merchant.default_terms = terms(network="xrpl:0")
    for _ in range(5):                                   # 5 次被拒：都不算
        assert fetch(merchant).verdict == "DENY"
    merchant.default_terms = terms()
    assert fetch(merchant).verdict == "AUTO"
    assert fetch(merchant).verdict == "AUTO"
    third = fetch(merchant)                              # 第 3 次真正付款才触发 R6
    assert (third.verdict, third.rule_id) == ("HOLD", "R6")
    assert offline.calls == 2


# ---- demo reset ----

def test_demo_reset_requires_demo_mode(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    assert TestClient(api.app).post("/api/demo/reset").status_code == 403


def test_demo_reset_clears_local_state(merchant, offline, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("MERCHANT_BASE_URL", merchant.base)   # 假商户没有 /data/d/reset，返回 merchant_d_reset=False
    monkeypatch.setattr(hot_wallet, "refill_for_demo", lambda: {"ok": True, "balance_xrp": "20"})   # 不碰链（见 test_demo_reset.py）
    fetch(merchant)
    merchant.default_terms = terms(8_000_000)
    fetch(merchant)

    resp = TestClient(api.app).post("/api/demo/reset")

    assert resp.status_code == 200
    with connect() as conn:
        for table in ("decisions", "approvals", "payment_requests", "budget_reservations"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# ---- 金额全部是整数 drops ----

@pytest.mark.parametrize("xrp, drops", [(0.01, 10_000), ("8", 8_000_000), (50, 50_000_000), ("0.000001", 1)])
def test_xrp_to_drops_is_exact(xrp, drops):
    assert policy.xrp_to_drops(xrp) == drops and isinstance(policy.xrp_to_drops(xrp), int)


def test_sub_drop_amount_rejected():
    with pytest.raises(ValueError):
        policy.xrp_to_drops("0.0000001")


def test_policy_amounts_are_ints(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    cfg = policy.load_policy()
    assert all(isinstance(getattr(cfg, f), int) for f in
               ("hard_cap_drops", "auto_limit_drops", "daily_budget_drops", "daily_total_cap_drops"))


def test_non_integer_drops_quote_denied(merchant, offline):
    merchant.default_terms = {**terms(), "amount": "10000.5"}
    r = fetch(merchant)
    assert (r.verdict, r.rule_id) == ("DENY", "R0")
    assert offline.calls == 0


# ---- 审批有效期配置 ----

@pytest.mark.parametrize("demo, override, expected", [
    ("false", None, 120), ("true", None, 300), ("true", "45", 45), ("false", "45", 45)])
def test_approval_ttl_resolution(tmp_path, monkeypatch, demo, override, expected):
    write_policy(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_MODE", demo)
    if override:
        monkeypatch.setenv("APPROVAL_TTL_SECONDS", override)
    else:
        monkeypatch.delenv("APPROVAL_TTL_SECONDS", raising=False)
    assert policy.load_policy().approval_ttl_seconds == expected
