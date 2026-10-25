"""验收条件（描述、dataSchema、交付条件）纳入 quote_hash；验货只用审批单里批准时的快照。

只改描述或只改 schema 都必须在付款前被判 R8 拒绝，不签名、不付款、不锁款。
"""

import asyncio

import pytest
from x402_xrpl.types import PaymentRequirements, ResourceInfo

from guard import approval_service, approvals, delivery_verifier, payments
from guard import state_machine as sm
from guard.quote import Quote
from merchants import escrow_merchant
from fakes import CountingSigner, fake_chain, fetch, merchant, offline, terms, write_policy  # noqa: F401


@pytest.fixture(autouse=True)
def escrow_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    write_policy(tmp_path, monkeypatch, escrow_threshold_xrp=5, escrow_cancel_after_seconds=600,
                 hot_wallet_max_balance_xrp=20)


def approve(approval_id):
    treasury = CountingSigner()
    return asyncio.run(approval_service.approve(approval_id, treasury)), treasury


def assert_denied_R8(view, treasury, payment_id):
    assert view["status"] == "rejected" and view["payment_state"] == sm.DENIED
    assert payments.get(payment_id)["rule_id"] == "R8"
    assert treasury.calls == 0


# ---- 规范化：只去排版差异，内容变化必改哈希 ----

def quote(description="XRPL data", schema=None, mime="application/json"):
    extra = {"invoiceId": "A" * 32, **({"dataSchema": schema} if schema else {})}
    req = PaymentRequirements(scheme="exact", network="xrpl:1", amount="10", asset="XRP", pay_to="rX",
                              max_timeout_seconds=600, extra=extra)
    return Quote("http://m/d", req, ResourceInfo(url="http://m/d", description=description, mime_type=mime))


def test_hash_normalization():
    base = quote(schema={"type": "object", "required": ["a", "b"]})
    assert base.terms_hash() == quote("  XRPL   data ", schema={"required": ["a", "b"], "type": "object"}).terms_hash()
    assert base.terms_hash() != quote("XRPL data!", schema={"type": "object", "required": ["a", "b"]}).terms_hash()
    assert base.terms_hash() != quote(schema={"type": "object", "required": ["a"]}).terms_hash()
    assert base.terms_hash() != quote(schema={"type": "object", "required": ["a", "b"]}, mime="text/csv").terms_hash()


# ---- 普通 x402：只改描述 → R8 ----

def test_description_change_after_hold_is_R8(merchant, offline):
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)
    assert (r.verdict, r.rule_id) == ("HOLD", "R4")

    merchant.resource_description = "fake data (now with different guarantees)"
    view, treasury = approve(r.approval_id)

    assert_denied_R8(view, treasury, r.payment_id)
    assert merchant.payment_headers == [] and offline.calls == 0


# ---- escrow：只改描述 / 只改 schema → R8，不锁款 ----

@pytest.mark.parametrize("change", ["description", "schema"])
def test_escrow_acceptance_change_is_R8(merchant, offline, fake_chain, monkeypatch, change):
    r = fetch(merchant, "/data/e")
    assert (r.verdict, r.state) == ("HOLD", sm.PENDING_APPROVAL)

    if change == "description":
        monkeypatch.setattr(escrow_merchant, "DESCRIPTION", escrow_merchant.DESCRIPTION + " Values may be estimated.")
    else:
        schema = {**escrow_merchant.DATA_SCHEMA, "required": escrow_merchant.DATA_SCHEMA["required"][:1]}
        monkeypatch.setattr(escrow_merchant, "DATA_SCHEMA", schema)
    view, treasury = approve(r.approval_id)

    assert_denied_R8(view, treasury, r.payment_id)
    assert fake_chain.of_type("EscrowCreate") == [] and merchant.escrow_proofs == []


def test_verifier_uses_approved_snapshot(merchant, offline, fake_chain, monkeypatch):
    """付款前商户给的描述只差排版（哈希相同、不触发 R8），验货拿到的仍是批准时快照里的原文。"""
    seen = []
    monkeypatch.setattr(delivery_verifier, "_llm_enabled", lambda: True)
    monkeypatch.setattr(delivery_verifier, "_llm_verify",
                        lambda description, delivery: seen.append(description) or {"verdict": "pass", "reason": "ok",
                                                                                    "evidence": ["x"]})
    approved_text = escrow_merchant.DESCRIPTION
    r = fetch(merchant, "/data/e")
    snapshot = approvals.acceptance(approvals.get(r.approval_id))
    assert snapshot["description"] == approved_text and snapshot["dataSchema"] == escrow_merchant.data_schema()
    assert approval_service.view(r.approval_id)["acceptance"] == snapshot          # 审批人看到的就是这份

    monkeypatch.setattr(escrow_merchant, "DESCRIPTION", "  " + approved_text.replace(" ", "   ") + "\n")
    view, _ = approve(r.approval_id)

    assert view["payment_state"] == sm.RESOURCE_DELIVERED
    assert len(seen) == 1 and seen[0].startswith(approved_text + "\nAcceptance period (frozen at approval)")
