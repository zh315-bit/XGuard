"""先验货再放款（escrow）+ 热钱包封顶 / 补款。全部离线：假商户 + 假链 + 离线签名。

“没有放款”的判据：假链上没有 EscrowFinish；“退款”的判据：有 EscrowCancel 且预算预留被释放。
"""

import asyncio
import json

import pytest

from guard import approval_service, budget, delivery_verifier, escrow, hot_wallet, payments
from guard import state_machine as sm
from fakes import (PAYEE, CountingSigner, fake_chain, fetch, merchant, offline, write_policy)  # noqa: F401

E_PRICE = 6_000_000


@pytest.fixture(autouse=True)
def escrow_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    write_policy(tmp_path, monkeypatch, escrow_threshold_xrp=5, escrow_cancel_after_seconds=600,
                 hot_wallet_max_balance_xrp=20)


def llm_says(monkeypatch, verdict):
    """替代验货的 LLM：返回固定结论（None = LLM 不可用）。"""
    calls = []

    def fake(description, delivery):
        calls.append(delivery)
        return None if verdict is None else {"verdict": verdict, "reason": f"LLM 判 {verdict}", "evidence": ["x"]}
    monkeypatch.setattr(delivery_verifier, "_llm_verify", fake)
    monkeypatch.setattr(delivery_verifier, "_llm_enabled", lambda: True)
    return calls


def buy_e(merchant):
    """请求 /data/e → HOLD → 批准（金库签 EscrowCreate）→ 取货 → 验货。返回 (fetch 结果, 审批视图, 金库签名器)。"""
    r = fetch(merchant, "/data/e")
    assert (r.verdict, r.rule_id, r.state) == ("HOLD", "R4", sm.PENDING_APPROVAL)
    assert payments.get(r.payment_id)["method"] == "escrow"
    treasury = CountingSigner()
    view = asyncio.run(approval_service.approve(r.approval_id, treasury))
    return r, view, treasury


def memo_of(entry):
    return json.loads(bytes.fromhex(entry["tx"]["Memos"][-1]["Memo"]["MemoData"]).decode())


def test_verification_pass_releases(merchant, offline, fake_chain, monkeypatch):
    llm_says(monkeypatch, "pass")

    r, view, treasury = buy_e(merchant)

    assert (view["status"], view["payment_state"]) == ("approved", sm.RESOURCE_DELIVERED)
    assert treasury.calls == 1                                         # 金库只签了 EscrowCreate
    create, = fake_chain.of_type("EscrowCreate")
    finish, = fake_chain.of_type("EscrowFinish")
    assert create["tx"]["Amount"] == str(E_PRICE) and create["tx"]["Destination"] == PAYEE
    assert create["tx"]["CancelAfter"] == fake_chain.time + 600 and create["tx"]["LastLedgerSequence"] == 1_000
    assert memo_of(create)["escrow"] == "lock"
    record = escrow.get(r.payment_id)
    assert finish["tx"]["Fulfillment"] == escrow.fulfillment_for(record["preimage"])
    assert escrow.condition_for(record["preimage"]) == create["tx"]["Condition"]
    assert memo_of(finish)["verification_hash"] == record["verification_hash"] == \
        delivery_verifier.verification_hash(json.loads(record["verification_json"]))
    assert merchant.escrow_proofs[0]["X-ESCROW-SEQUENCE"] == str(create["tx"]["Sequence"])
    assert budget.get(r.payment_id)["status"] == "spent"
    assert "preimage" not in escrow.public_view(record)                # 原像绝不外泄


@pytest.mark.parametrize("mode, verdict, source", [
    ("garbage", "pass", "deterministic"),          # 2019 年数据：日期不在报价周内，确定性检查直接 fail，LLM 说 pass 也没用
    ("malformed", "pass", "deterministic"),        # daily 为空：同上
], ids=["garbage", "malformed"])
def test_verification_fail_refunds_after_cancel_after(merchant, offline, fake_chain, monkeypatch, mode, verdict, source):
    merchant.e_mode = mode
    llm_calls = llm_says(monkeypatch, verdict)

    r, view, _ = buy_e(merchant)

    assert view["payment_state"] == sm.REFUND_PENDING
    assert fake_chain.of_type("EscrowFinish") == []                    # 没有放款
    verification = view["escrow"]["verification"]
    assert verification["verdict"] == "fail" and verification["source"] == source
    assert llm_calls == []                                             # 确定性检查不过就不问 LLM

    fake_chain.time = view["escrow"]["cancel_after"] + 1
    escrow.sweep()

    cancel, = fake_chain.of_type("EscrowCancel")
    assert memo_of(cancel)["verification"] == "fail"
    assert memo_of(cancel)["verification_hash"] == escrow.get(r.payment_id)["verification_hash"]
    assert payments.get(r.payment_id)["state"] == sm.REFUNDED
    assert budget.get(r.payment_id)["status"] == "released"


def test_llm_unavailable_releases_with_note(merchant, offline, fake_chain, monkeypatch):
    """七天 DEX 数据的验收条件都能确定性检查：正常数据 + LLM 不可用 → 放款，结论注明语义检查未完成。"""
    llm_says(monkeypatch, None)

    r, view, _ = buy_e(merchant)

    verification = view["escrow"]["verification"]
    assert verification["verdict"] == "pass" and "semantic check not completed" in verification["reason"]
    assert verification["semantic_check"] == "incomplete"
    assert all(c["ok"] for c in verification["checks"])
    assert view["payment_state"] == sm.RESOURCE_DELIVERED and len(fake_chain.of_type("EscrowFinish")) == 1


def test_llm_unavailable_garbage_still_refunds(merchant, offline, fake_chain, monkeypatch):
    merchant.e_mode = "garbage"
    llm_says(monkeypatch, None)

    r, view, _ = buy_e(merchant)

    assert view["escrow"]["verification"]["verdict"] == "fail"
    assert view["payment_state"] == sm.REFUND_PENDING and fake_chain.of_type("EscrowFinish") == []


def test_llm_fail_can_still_reject_normal_data(merchant, offline, fake_chain, monkeypatch):
    llm_says(monkeypatch, "fail")                                      # 语义检查是额外的一道：可以多拒
    r, view, _ = buy_e(merchant)
    assert view["escrow"]["verification"]["source"] == "deterministic+llm"
    assert view["payment_state"] == sm.REFUND_PENDING


def test_cannot_cancel_before_cancel_after(merchant, offline, fake_chain, monkeypatch):
    merchant.e_mode = "malformed"
    llm_says(monkeypatch, "fail")
    r, view, _ = buy_e(merchant)
    cancel_after = view["escrow"]["cancel_after"]

    fake_chain.time = cancel_after                                     # 恰好等于也不行（必须严格大于）
    with pytest.raises(escrow.TooEarlyToCancel):
        escrow.refund(r.payment_id)
    escrow.sweep()
    assert fake_chain.of_type("EscrowCancel") == [] and payments.get(r.payment_id)["state"] == sm.REFUND_PENDING

    fake_chain.time = cancel_after + 1
    escrow.refund(r.payment_id)
    assert len(fake_chain.of_type("EscrowCancel")) == 1


def test_refund_is_not_duplicated(merchant, offline, fake_chain, monkeypatch):
    merchant.e_mode = "malformed"
    llm_says(monkeypatch, "fail")
    r, view, _ = buy_e(merchant)
    fake_chain.time = view["escrow"]["cancel_after"] + 1
    escrow.sweep()
    escrow.sweep()
    escrow.refund(r.payment_id)
    assert len(fake_chain.of_type("EscrowCancel")) == 1


def test_small_exact_purchase_still_uses_x402(merchant, offline, fake_chain):
    """B 这类只支持 exact 的大额购买不受影响：仍是审批后直接付款，不走 escrow。"""
    from fakes import terms
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)
    assert payments.get(r.payment_id)["method"] == "x402"


def test_agent_view_for_rejected_delivery(merchant, offline, fake_chain, monkeypatch):
    from guard import views
    merchant.e_mode = "garbage"
    llm_says(monkeypatch, "fail")
    r, _, _ = buy_e(merchant)

    agent_view = views.approval_for_agent(approval_service.view(r.approval_id))

    assert agent_view["status"] == "delivery_rejected" and "Verification failed" in agent_view["reason"]
    assert "content" not in agent_view                                 # 垃圾数据不交给 agent
    assert agent_view["escrow"]["verification"] == "fail"


# ---- 确定性检查 ----

def test_deterministic_checks():
    from merchants import escrow_merchant
    schema = escrow_merchant.data_schema()
    ok = delivery_verifier.deterministic_checks(schema, escrow_merchant._content("normal"))
    assert all(c["ok"] for c in ok)
    for bad in ("not json", {}, {"dataset": "x"}, escrow_merchant._content("malformed"),
                {**escrow_merchant._content("normal"), "daily": [{"date": "昨天", "volume_xrp": 1}] * 7},
                {**escrow_merchant._content("normal"), "daily": [{"date": "2026-10-01", "volume_xrp": -5}] * 7}):
        assert not all(c["ok"] for c in delivery_verifier.deterministic_checks(schema, bad)), bad


def test_llm_pass_cannot_override_failed_checks(monkeypatch):
    from merchants import escrow_merchant
    llm_says(monkeypatch, "pass")
    out = delivery_verifier.verify("x", escrow_merchant.data_schema(), {"dataset": "x"})
    assert out["verdict"] == "fail"


# ---- 热钱包封顶 / 补款 ----

def test_excess_to_return():
    assert hot_wallet.excess_to_return(100_000_000, 20_000_000) == 80_000_000
    assert hot_wallet.excess_to_return(19_000_000, 20_000_000) == 0


def test_topup_over_cap_is_refused(fake_chain, monkeypatch):
    from guard import chain
    monkeypatch.setattr(chain, "balance_drops", lambda address: 15_000_000)
    with pytest.raises(hot_wallet.CapExceeded):
        hot_wallet.request_topup(6_000_000, "test")                    # 15 + 6 > 20


def test_topup_needs_approval_and_rechecks_cap(fake_chain, monkeypatch):
    from guard import chain
    balance = {"drops": 15_000_000}
    monkeypatch.setattr(chain, "balance_drops", lambda address: balance["drops"])

    created = hot_wallet.request_topup(4_000_000, "test")              # 15 + 4 ≤ 20：生成审批单，不付款
    assert fake_chain.submitted == []
    view = approval_service.view(created["approval_id"])
    assert (view["status"], view["rule_id"], view["method"]) == ("pending", "TOPUP", "topup")

    balance["drops"] = 17_000_000                                       # 审批期间余额涨了：17 + 4 > 20
    treasury = CountingSigner()
    view = asyncio.run(approval_service.approve(created["approval_id"], treasury))

    assert view["payment_state"] == sm.FAILED and "above the cap" in view["reject_reason"]
    assert treasury.calls == 0 and fake_chain.submitted == []


def test_topup_executes_after_approval(fake_chain, monkeypatch):
    from guard import chain
    monkeypatch.setattr(chain, "balance_drops", lambda address: 15_000_000)
    created = hot_wallet.request_topup(4_000_000, "test")
    treasury = CountingSigner()

    view = asyncio.run(approval_service.approve(created["approval_id"], treasury))

    payment, = fake_chain.of_type("Payment")
    assert view["payment_state"] == sm.RESOURCE_DELIVERED and treasury.calls == 1
    assert payment["tx"]["Amount"] == "4000000" and payment["tx"]["Account"] == treasury.address
    assert budget.get(created["payment_id"]) is None                   # 内部划转不占日预算
