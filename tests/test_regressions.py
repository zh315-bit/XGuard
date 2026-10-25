"""最近一次 demo_run 暴露的问题的回归测试。

1. 付款成功后必须拿到内容：只有真正拿到内容才算 paid；商户回 409（facilitator 结算状态未知）时，
   不重发、按哈希查链，结果如实标为“已付款、资源未送达”，绝不显示成 paid
2. 付款方标签以实际付款账户为准：人工批准 = 金库，哪怕结果是经对账才确认的
3. escrow 锁款在签名阶段失败：escrow 卡片状态与支付状态一致（failed，不是“准备锁款”）
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from guard import api, approval_service, escrow, views
from guard import state_machine as sm
from guard.settlement import ChainStatus
from fakes import CountingSigner, fake_chain, fetch, merchant, offline, terms, write_policy  # noqa: F401
from test_matrix import chain  # noqa: F401  (fixture)


def test_paid_means_content_delivered(merchant, offline):
    """AUTO 和人工批准两条路径：status=paid 时 content 必须是商户交付的内容。"""
    auto = views.present(fetch(merchant, "/a"))
    assert auto["status"] == "paid" and auto["content"] == {"ok": True, "path": "/a"}

    merchant.default_terms = terms(8_000_000)
    held = fetch(merchant, "/b")
    view = asyncio.run(approval_service.approve(held.approval_id, CountingSigner()))
    assert view["payment_state"] == sm.RESOURCE_DELIVERED and view["result"] == {"ok": True, "path": "/b"}


@pytest.mark.parametrize("approved", [False, True], ids=["auto", "approved"])
def test_settlement_unknown_409_is_never_reported_as_paid(merchant, offline, chain, approved):
    merchant.pay_mode = "settle_unknown"
    chain.status = ChainStatus(found=True, validated=True, success=True, result_code="tesSUCCESS")
    if approved:
        merchant.default_terms = terms(8_000_000)
        held = fetch(merchant, request_id="r409")
        asyncio.run(approval_service.approve(held.approval_id, CountingSigner()))
    r = fetch(merchant, request_id="r409")

    out = views.present(r)
    assert out["status"] == "paid_resource_missing" and "content" not in out
    assert r.state == sm.VALIDATED and r.paid                       # 钱确实付了（链上确认）
    assert len(merchant.payment_headers) == 1                       # 只发了一次，没有重发

    audit_rows = {d["id"]: d for d in TestClient(api.app).get("/audit").json()}
    payer = audit_rows[r.decision_id]["payer"]
    assert payer == ("金库（Ledger 签名）" if approved else "热钱包")
    assert audit_rows[r.decision_id]["status"] == ("approved_paid" if approved else "paid")


def test_demo_merchant_waits_long_enough_for_settlement():
    """facilitator /settle 要等上链；默认 30 秒在网络抖动时不够，演示商户放宽了超时。"""
    from merchants import demo
    assert demo.FACILITATOR_TIMEOUT_SECONDS >= 60


def test_escrow_card_shows_failed_when_signing_fails(merchant, offline, fake_chain, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    write_policy(tmp_path, monkeypatch, escrow_threshold_xrp=5)

    class RejectingSigner(CountingSigner):
        async def sign(self, tx):
            raise RuntimeError("XrpAppCommandError: 6985: Condition of use not satisfied (Rejected by user)")

    r = fetch(merchant, "/data/e")
    view = asyncio.run(approval_service.approve(r.approval_id, RejectingSigner()))

    card = escrow.public_view(escrow.get(r.payment_id))
    assert view["payment_state"] == sm.FAILED
    assert card["status"] == "failed" and "6985" in card["payment_error"]
    assert fake_chain.of_type("EscrowCreate") == []                  # 没有锁款
