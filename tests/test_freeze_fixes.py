"""冻结前的两处修复：商户 D 按买方分别计数调包；R3 审批必须输入收款地址后 4 位才能批准。"""

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from guard import approval_service, payment_executor
from guard.approval_service import ApprovalError
from fakes import OTHER, CountingSigner, fetch, merchant, offline, terms  # noqa: F401


# ---- 商户 D：调包计数按 agent_id（买方头）分别计数 ----

@pytest.fixture
def demo_client():
    from merchants import demo
    demo.swap_counts.clear()
    yield demo, TestClient(demo.app)
    demo.swap_counts.clear()


def quote_pay_to(client, agent_id):
    resp = client.get("/data/d", headers=payment_executor.buyer_headers(agent_id))
    assert resp.status_code == 402
    return resp.json()["accepts"][0]["payTo"]


def test_swap_counter_is_per_agent(demo_client):
    demo, client = demo_client
    honest, swapped = os.environ["MERCHANT_A_ADDRESS"], os.environ["MERCHANT_C_ADDRESS"]

    assert quote_pay_to(client, "mission-1") == honest      # 新任务第一次报价：正常地址
    assert quote_pay_to(client, "mission-1") == swapped     # 付款前复查：被调包 → 守卫判 R8
    assert quote_pay_to(client, "mission-2") == honest      # 另一个任务互不影响
    assert quote_pay_to(client, "mission-2") == swapped


def test_reset_clears_all_counters(demo_client):
    demo, client = demo_client
    for agent in ("mission-1", "mission-2"):
        quote_pay_to(client, agent)
    assert client.post("/data/d/reset").status_code == 200
    assert demo.swap_counts == {}
    assert quote_pay_to(client, "mission-1") == os.environ["MERCHANT_A_ADDRESS"]


def test_guard_sends_buyer_header(merchant, offline):
    fetch(merchant)
    expected = payment_executor.buyer_headers("test-agent")[payment_executor.BUYER_HEADER]
    assert len(merchant.buyer_headers) >= 2 and set(merchant.buyer_headers) == {expected}  # 询价、复查、付款都带


# ---- R3：收款方未认证，批准前要输入收款地址后 4 位 ----

def test_R3_approval_requires_payee_suffix(merchant, offline):
    merchant.default_terms = terms(pay_to=OTHER)
    r = fetch(merchant)
    assert (r.verdict, r.rule_id) == ("HOLD", "R3")
    assert approval_service.view(r.approval_id)["payee_unverified"] is True

    treasury = CountingSigner()
    for suffix in (None, "", "ABCD", OTHER[-5:]):
        with pytest.raises(ApprovalError) as exc:
            asyncio.run(approval_service.approve(r.approval_id, treasury, confirm_payee_suffix=suffix))
        assert exc.value.status_code == 400 and "phishing" in str(exc.value)
    assert treasury.calls == 0 and merchant.payment_headers == []
    assert approval_service.view(r.approval_id)["status"] == "pending"   # 输错不会消耗审批单

    view = asyncio.run(approval_service.approve(r.approval_id, treasury, confirm_payee_suffix=OTHER[-4:]))
    assert view["status"] == "approved" and treasury.calls == 1


def test_non_R3_approval_needs_no_suffix(merchant, offline):
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)
    assert r.rule_id == "R4" and approval_service.view(r.approval_id)["payee_unverified"] is False
    assert asyncio.run(approval_service.approve(r.approval_id, CountingSigner()))["status"] == "approved"
