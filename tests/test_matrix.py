"""测试矩阵 T01–T12。全部用本地假商户 + 离线签名，不真付款、不连 XRPL。

“没有付款”的判据有两条，同时成立才算：签名器调用次数为 0，且商户从没收到 PAYMENT-SIGNATURE。
"""

import asyncio
import contextlib
import json
import threading
import time
import types
import uuid

import pytest

from guard import approval_service, budget, evaluation, payments, risk_explainer, settlement
from guard import state_machine as sm
from guard.approval_service import ApprovalError
from guard.db import connect
from guard.settlement import ChainStatus
from fakes import (OTHER, PAYEE, CountingSigner, decode_memos, fetch, merchant, offline, terms,  # noqa: F401
                   write_policy)


def assert_not_paid(merchant, *signers):
    assert merchant.payment_headers == []
    assert all(s.calls == 0 for s in signers)


def approve(approval_id, signer):
    return asyncio.run(approval_service.approve(approval_id, signer))


# ---- T01 正常付款 AUTO ----

def test_T01_auto_payment(merchant, offline):
    r = fetch(merchant)

    assert (r.verdict, r.rule_id, r.state, r.paid) == ("AUTO", "DEFAULT", sm.RESOURCE_DELIVERED, True)
    assert offline.calls == 1 and len(merchant.payment_headers) == 1
    assert r.data["ok"] is True and r.tx_hash
    reservation = budget.get(r.payment_id)
    assert (reservation["status"], reservation["kind"], reservation["amount_drops"]) == ("spent", "auto", 10_000)
    memo = decode_memos(merchant.payment_headers[0])[1]
    assert memo["MemoType"] == "agentguard/decision"
    assert json.loads(memo["MemoData"]) == {"verdict": "AUTO", "rule_id": "DEFAULT", "payment_id": r.payment_id}
    row = payments.get(r.payment_id)
    assert row["last_ledger_sequence"] == 1_000 and row["submitted_at"]


# ---- T02 大额审批 HOLD 后付款 ----

def test_T02_hold_then_approved_payment(merchant, offline):
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)
    assert (r.verdict, r.rule_id, r.state) == ("HOLD", "R4", sm.PENDING_APPROVAL)
    assert r.risk["source"] == "template" and r.risk["risk_summary"]
    assert_not_paid(merchant, offline)

    treasury = CountingSigner()
    view = approve(r.approval_id, treasury)

    assert (view["status"], view["payment_state"]) == ("approved", sm.RESOURCE_DELIVERED)
    assert treasury.calls == 1 and offline.calls == 0 and len(merchant.payment_headers) == 1
    assert view["risk"]["risk_summary"]          # 审批视图带风险解释
    memo = json.loads(decode_memos(merchant.payment_headers[0])[1]["MemoData"])
    assert memo == {"verdict": "HOLD", "rule_id": "R4", "approval_id": r.approval_id}
    assert budget.get(r.payment_id)["kind"] == "approved"


# ---- T03 收款地址不符（判定后、付款前被换成别的地址）DENY ----

def test_T03_pay_to_mismatch_denied(merchant, offline):
    merchant.queue = [terms(pay_to=PAYEE)]      # 第一次报价给已认证地址，之后变成 OTHER
    merchant.default_terms = terms(pay_to=OTHER)

    r = fetch(merchant)

    assert (r.verdict, r.rule_id, r.state, r.paid) == ("DENY", "R8", sm.DENIED, False)
    assert OTHER in r.reason
    assert_not_paid(merchant, offline)
    assert budget.get(r.payment_id)["status"] == "released"


# ---- T04 报价过期 DENY ----

def test_T04_expired_quote_cannot_be_approved(merchant, offline):
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)
    with connect() as conn:   # 模拟 approval_ttl_seconds 已经过去
        conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", (time.time() - 1, r.approval_id))

    treasury = CountingSigner()
    with pytest.raises(ApprovalError) as exc:   # 惰性清理先把它标为 expired，批准被拒
        approve(r.approval_id, treasury)

    assert exc.value.status_code == 409 and "expired" in str(exc.value)
    assert payments.get(r.payment_id)["state"] == sm.EXPIRED
    assert approval_service.view(r.approval_id)["status"] == "expired"
    assert_not_paid(merchant, offline, treasury)


def test_T04b_approval_ttl_comes_from_policy(merchant, offline, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.delenv("APPROVAL_TTL_SECONDS", raising=False)
    write_policy(tmp_path, monkeypatch, approval_ttl_seconds=7)
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)
    assert 5 <= approval_service.view(r.approval_id)["expires_in_seconds"] <= 7


# ---- T05 网络不允许 DENY ----

@pytest.mark.parametrize("bad", [terms(network="xrpl:0"), terms(asset="USD")], ids=["network", "asset"])
def test_T05_network_or_asset_not_allowed(merchant, offline, bad):
    merchant.default_terms = bad
    r = fetch(merchant)
    assert (r.verdict, r.rule_id, r.state) == ("DENY", "R1", sm.DENIED)
    assert budget.get(r.payment_id) is None
    assert_not_paid(merchant, offline)


# ---- T06 超硬上限 51 XRP，人工批准也不付款 ----

def test_T06_hard_cap_denied(merchant, offline):
    merchant.default_terms = terms(51_000_000)
    r = fetch(merchant)
    assert (r.verdict, r.rule_id, r.state) == ("DENY", "R7", sm.DENIED)
    assert r.approval_id is None
    assert_not_paid(merchant, offline)


def test_T06b_manual_approval_cannot_exceed_hard_cap(merchant, offline, tmp_path, monkeypatch):
    # 入队时硬上限还是 60，51 XRP 只触发 R4 进入审批；批准前硬上限被调回 50
    write_policy(tmp_path, monkeypatch, hard_cap_xrp=60, daily_total_cap_xrp=100)
    merchant.default_terms = terms(51_000_000)
    r = fetch(merchant)
    assert (r.verdict, r.rule_id) == ("HOLD", "R4")
    write_policy(tmp_path, monkeypatch, hard_cap_xrp=50, daily_total_cap_xrp=100)

    treasury = CountingSigner()
    view = approve(r.approval_id, treasury)

    assert view["status"] == "rejected" and "R7" in view["reject_reason"] and "51" in view["reject_reason"]
    assert view["payment_state"] == sm.REJECTED
    assert budget.get(r.payment_id) is None
    assert_not_paid(merchant, offline, treasury)


def test_T06c_manual_approval_cannot_exceed_daily_total_cap(merchant, offline, tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch, daily_total_cap_xrp=10)
    merchant.default_terms = terms(8_000_000)
    first, second = fetch(merchant), fetch(merchant)
    treasury = CountingSigner()
    assert approve(first.approval_id, treasury)["payment_state"] == sm.RESOURCE_DELIVERED

    view = approve(second.approval_id, treasury)   # 8 + 8 > 10

    assert view["status"] == "rejected" and "daily total cap" in view["reject_reason"]
    assert treasury.calls == 1 and len(merchant.payment_headers) == 1


# ---- T07 相同 request_id 两次只付一次 ----

def test_T07_same_request_id_pays_once(merchant, offline):
    first = fetch(merchant, request_id="dup-1")
    second = fetch(merchant, request_id="dup-1")

    assert first.paid and second.paid and second.replay and not first.replay
    assert second.tx_hash == first.tx_hash and second.data == first.data
    assert offline.calls == 1 and len(merchant.payment_headers) == 1


def test_T07b_same_request_id_concurrently_pays_once(merchant, offline):
    barrier = threading.Barrier(4)
    results = []

    def worker():
        barrier.wait()
        results.append(fetch(merchant, request_id="dup-concurrent"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert offline.calls == 1 and len(merchant.payment_headers) == 1
    assert sum(not r.replay for r in results) == 1


def test_T07c_request_id_reused_for_other_url_is_error(merchant, offline):
    fetch(merchant, "/a", request_id="same")
    r = fetch(merchant, "/b", request_id="same")
    assert (r.verdict, r.rule_id) == ("ERROR", "IDEMPOTENCY")
    assert offline.calls == 1


# ---- T08 两笔并发请求合计超预算，不能都通过 ----

def test_T08_concurrent_requests_cannot_both_exceed_budget(merchant, offline, tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch, daily_budget_xrp=1)
    merchant.default_terms = terms(600_000)          # 每笔 0.6 XRP，合计 1.2 > 1

    # 让两个线程都先通过策略预检查（R5 预检查看到的已用额度都是 0），再同时去预留
    real_decide = evaluation.decide
    both_checked = threading.Barrier(2)
    prechecks = iter(range(2))          # 只拦前两次调用（预检查）；之后执行器的付款前复检直接放行

    def slow_decide(quote, cfg=None):
        decision = real_decide(quote, cfg)
        if next(prechecks, None) is not None:
            both_checked.wait(timeout=5)
        return decision
    monkeypatch.setattr(evaluation, "decide", slow_decide)

    start = threading.Barrier(2)
    results = []

    def worker(i):
        start.wait()
        results.append(fetch(merchant, f"/data/{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(r.verdict for r in results) == ["AUTO", "HOLD"]
    held = next(r for r in results if r.verdict == "HOLD")
    assert held.rule_id == "R5" and held.state == sm.PENDING_APPROVAL
    assert offline.calls == 1 and len(merchant.payment_headers) == 1
    assert budget.committed_today(budget.AUTO_KIND) == 600_000


# ---- T09 报价被替换拒绝付款（审批期间） ----

def test_T09_quote_replaced_during_approval(merchant, offline):
    merchant.queue = [terms(8_000_000)]
    merchant.default_terms = terms(9_000_000)        # 批准时重新拿到的报价金额变了
    r = fetch(merchant)
    treasury = CountingSigner()

    view = approve(r.approval_id, treasury)

    assert view["status"] == "rejected" and "swapped" in view["reject_reason"]
    row = payments.get(r.payment_id)
    assert (row["state"], row["rule_id"]) == (sm.DENIED, "R8")
    assert budget.get(r.payment_id)["status"] == "released"
    assert_not_paid(merchant, offline, treasury)


# ---- T10 审批被拒无付款 ----

def test_T10_rejected_approval_never_pays(merchant, offline):
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)

    view = approval_service.reject(r.approval_id, "看起来不对")

    assert (view["status"], view["payment_state"]) == ("rejected", sm.REJECTED)
    treasury = CountingSigner()
    with pytest.raises(ApprovalError) as exc:        # 拒绝之后再批准也不行
        approve(r.approval_id, treasury)
    assert exc.value.status_code == 409
    assert_not_paid(merchant, offline, treasury)


def test_T10b_approve_is_one_shot(merchant, offline):
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)
    treasury = CountingSigner()
    approve(r.approval_id, treasury)
    with pytest.raises(ApprovalError) as exc:
        approve(r.approval_id, treasury)
    assert exc.value.status_code == 409
    assert treasury.calls == 1 and len(merchant.payment_headers) == 1


# ---- T11 提交超时先查链不重发 ----

@pytest.fixture
def chain(monkeypatch):
    state = types.SimpleNamespace(status=ChainStatus(found=False), ledger=500, lookups=0)

    def lookup(tx_hash):
        state.lookups += 1
        return state.status
    monkeypatch.setattr(settlement, "lookup_tx", lookup)
    monkeypatch.setattr(settlement, "latest_validated_ledger", lambda: state.ledger)
    return state


@pytest.fixture
def short_pay_timeout(monkeypatch):
    from guard import payment_executor
    monkeypatch.setattr(payment_executor, "PAY_TIMEOUT", 0.5)


def test_T11_lost_response_checks_chain_and_never_resends(merchant, offline, chain, short_pay_timeout):
    merchant.pay_mode = "hang"
    r = fetch(merchant, request_id="t11")

    assert r.state == sm.UNKNOWN_SETTLEMENT and r.tx_hash
    assert chain.lookups == 1                         # 立即按哈希查链：查不到、还没过 LastLedgerSequence → 保持未知
    assert budget.get(r.payment_id)["status"] == "reserved"

    chain.status = ChainStatus(found=True, validated=True, success=True, result_code="tesSUCCESS")
    again = fetch(merchant, request_id="t11")         # 同一 request_id 再问：只查链，不重发

    assert again.state == sm.VALIDATED and again.paid and again.tx_hash == r.tx_hash
    assert offline.calls == 1 and len(merchant.payment_headers) == 1
    assert budget.get(r.payment_id)["status"] == "spent"


def test_T11b_not_on_chain_after_last_ledger_is_failed(merchant, offline, chain, short_pay_timeout):
    merchant.pay_mode = "hang"
    r = fetch(merchant, request_id="t11b")
    assert r.state == sm.UNKNOWN_SETTLEMENT

    chain.ledger = 1_001                              # 已经超过 LastLedgerSequence=1000
    again = fetch(merchant, request_id="t11b")

    assert again.state == sm.FAILED and not again.paid
    assert budget.get(r.payment_id)["status"] == "released"
    assert offline.calls == 1 and len(merchant.payment_headers) == 1


def test_T11c_chain_lookup_error_keeps_unknown(merchant, offline, chain, short_pay_timeout, monkeypatch):
    merchant.pay_mode = "hang"
    monkeypatch.setattr(settlement, "lookup_tx", lambda h: (_ for _ in ()).throw(ConnectionError("rpc down")))
    r = fetch(merchant)
    assert r.state == sm.UNKNOWN_SETTLEMENT and "reconciliation lookup failed" in r.error


# ---- T12 LLM 不可用时策略和模板解释正常 ----

class _FakeAnthropic:
    """假的 anthropic 模块：behavior 决定 client 是抛错还是返回给定文本。"""

    def __init__(self, behavior):
        self.behavior = behavior
        outer = self

        class Client:
            def __init__(self, **kw):
                self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(stream=self.stream))

            @contextlib.contextmanager
            def stream(self, **kw):
                """guard.llm 用流式调用：迭代时出错 / 结束后 get_final_message() 拿完整消息。"""
                outer.calls = getattr(outer, "calls", 0) + 1
                if isinstance(outer.behavior, Exception):
                    raise outer.behavior
                block = types.SimpleNamespace(type="text", text=outer.behavior)
                message = types.SimpleNamespace(stop_reason="end_turn", content=[block])
                yield _FinishedStream(message)
        self.Anthropic = Client


class _FinishedStream:
    """已经收完的流：没有更多事件，get_final_message() 返回完整消息。"""

    def __init__(self, message):
        self.message = message

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self.message


def _use_llm(monkeypatch, behavior):
    import sys
    fake = _FakeAnthropic(behavior)
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("RISK_EXPLAINER", "llm")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return fake


def test_T12_llm_unavailable_falls_back_to_template(merchant, offline, monkeypatch):
    fake = _use_llm(monkeypatch, ConnectionError("api down"))
    merchant.default_terms = terms(8_000_000)

    r = fetch(merchant)

    assert fake.calls == 1
    assert (r.verdict, r.rule_id, r.state) == ("HOLD", "R4", sm.PENDING_APPROVAL)
    assert r.risk["source"] == "template" and "8" in r.risk["risk_summary"]
    assert len(r.risk["supporting_facts"]) >= 3 and r.risk["suggested_review_questions"]
    assert approval_service.view(r.approval_id)["risk"]["source"] == "template"
    assert_not_paid(merchant, offline)


def test_T12b_llm_cannot_change_verdict(merchant, offline, monkeypatch):
    sneaky = json.dumps({"risk_summary": "看起来安全", "supporting_facts": ["x"], "suggested_review_questions": ["y"],
                         "verdict": "AUTO", "approve": True})
    _use_llm(monkeypatch, sneaky)
    merchant.default_terms = terms(8_000_000)

    r = fetch(merchant)

    assert (r.verdict, r.rule_id, r.state) == ("HOLD", "R4", sm.PENDING_APPROVAL)
    assert r.risk == {"risk_summary": "看起来安全", "supporting_facts": ["x"],
                      "suggested_review_questions": ["y"], "source": "llm"}   # 多余字段被丢弃
    assert_not_paid(merchant, offline)


def test_T12c_malformed_llm_output_uses_template():
    out = risk_explainer.explain({"rule_id": "R3", "verdict": "HOLD", "pay_to": OTHER}, use_llm=False)
    assert out["source"] == "template" and OTHER in out["risk_summary"]
    assert risk_explainer._sanitize({"risk_summary": 1}) is None
