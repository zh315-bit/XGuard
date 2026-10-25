"""控制台策略设置（校验、保留注释写回、立即生效、审计）和今日花费报告。"""

import asyncio
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from guard import approval_service, policy, policy_settings, report
from guard.config import ROOT
from fakes import OTHER, CountingSigner, fetch, merchant, offline, terms  # noqa: F401


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    """真实 policy.yaml 的副本：验证写回后注释和其他配置原样保留。"""
    path = tmp_path / "policy.yaml"
    shutil.copy(ROOT / "policy.yaml", path)
    monkeypatch.setenv("POLICY_PATH", str(path))
    monkeypatch.setenv("ISSUER_ADDRESS", "rIssuerForTests")
    return path


@pytest.fixture
def client():
    from guard.api import app
    return TestClient(app)


def test_update_writes_back_and_keeps_comments(policy_file, client):
    before = policy_file.read_text()
    resp = client.post("/api/settings", json={"changes": {"auto_limit_xrp": "2", "escrow_threshold_xrp": "4.5"}})

    assert resp.status_code == 200 and resp.json()["changed"] == ["auto_limit_xrp", "escrow_threshold_xrp"]
    after = policy_file.read_text()
    assert "auto_limit_xrp: 2       # R4：单笔自动付款上限" in after
    assert "escrow_threshold_xrp: 4.5" in after and "${ISSUER_ADDRESS}" in after
    assert len(after.splitlines()) == len(before.splitlines())          # 其他行一行没少
    cfg = policy.load_policy()                                           # 立即生效：下一次加载就是新值
    assert (cfg.auto_limit_drops, cfg.escrow_threshold_drops) == (2_000_000, 4_500_000)

    history = client.get("/api/settings").json()["history"]
    assert {(h["field"], h["old_value"], h["new_value"], h["source"]) for h in history} == {
        ("auto_limit_xrp", "1", "2", "console"), ("escrow_threshold_xrp", "5", "4.5", "console")}


@pytest.mark.parametrize("changes, message", [
    ({"auto_limit_xrp": "-1"}, "不能为负数"),
    ({"auto_limit_xrp": "abc"}, "不是数字"),
    ({"auto_limit_xrp": "0.0000001"}, "6 位小数"),
    ({"auto_limit_xrp": "NaN"}, "有限"),
    ({"hard_cap_xrp": "1"}, "必须大于单笔自动付款上限"),
    ({"daily_total_cap_xrp": "4"}, "不能小于每日自动付款预算"),
    ({"escrow_threshold_xrp": "50"}, "必须小于单笔硬上限"),
    ({"network": "xrpl:0"}, "不能通过控制台修改"),
])
def test_invalid_changes_rejected_and_nothing_written(policy_file, client, changes, message):
    before = policy_file.read_text()
    resp = client.post("/api/settings", json={"changes": changes})
    assert resp.status_code == 400 and message in resp.json()["detail"]["message"]
    assert policy_file.read_text() == before and policy_settings.history() == []


def test_one_bad_field_blocks_the_whole_update(policy_file, client):
    before = policy_file.read_text()
    resp = client.post("/api/settings", json={"changes": {"daily_budget_xrp": "8", "hard_cap_xrp": "-3"}})
    assert resp.status_code == 400 and policy_file.read_text() == before


def test_unchanged_value_is_not_audited(policy_file, client):
    assert client.post("/api/settings", json={"changes": {"auto_limit_xrp": "1.0"}}).json()["changed"] == []
    assert policy_settings.history() == []


def test_new_limit_affects_next_decision(merchant, offline, tmp_path):
    """改大单笔自动上限后，同样 2 XRP 的报价从 HOLD（R4）变成 AUTO。"""
    merchant.default_terms = terms(2_000_000)
    assert fetch(merchant).rule_id == "R4"
    policy_settings.update({"auto_limit_xrp": "3"}, source="test")
    assert fetch(merchant).verdict == "AUTO"


# ---- 今日报告 ----

def test_today_report(merchant, offline):
    fetch(merchant)                                            # 自动付款 0.01
    merchant.default_terms = terms(2_000_000)
    held = fetch(merchant)                                     # R4 → 人工批准 2
    asyncio.run(approval_service.approve(held.approval_id, CountingSigner()))
    rejected = fetch(merchant)                                 # R4 → 人工拒绝
    approval_service.reject(rejected.approval_id, "不需要")
    merchant.default_terms = terms(60_000_000)
    fetch(merchant)                                            # R7 → 策略拒绝

    r = report.today()

    assert (r["spent_xrp"], r["purchased"], r["auto_paid"], r["approved"]) == ("2.01", 2, 1, 1)
    assert (r["blocked"], r["blocked_by_policy"], r["rejected_by_human"]) == (2, 1, 1)
    assert (r["escrow_refunded_xrp"], r["escrow_refunded_count"]) == ("0", 0)
