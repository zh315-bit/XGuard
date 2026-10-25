"""x402 拦截：先探测报价 → 策略判定 → AUTO 预留预算并付款 / HOLD 入队 / DENY 退回。

每次调用对应 payment_requests 里的一行，按 state_machine 的合法路径推进。
(agent_id, request_id) 唯一：同一个 request_id 再来，直接返回已有结果，不会再付款。
"""

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

import requests
from x402_xrpl.types import PaymentRequirements

from guard import approvals, audit, budget, evaluation, maintenance, payment_executor, payments, policy, risk_explainer
from guard import state_machine as sm
from guard.budget import BudgetExceeded
from guard.policy import AUTO, DENY, HOLD, DROPS_PER_XRP, Decision, fmt_xrp
from guard.quote import Quote, QuoteError, parse_402
from guard.settlement import reconcile
from guard.signers import agent_signer
from guard.xrpl_client import explorer_tx_url

PROBE_TIMEOUT = 30
ESCROW_SCHEME = "escrow"
SUPPORTED_SCHEMES = frozenset({"exact", ESCROW_SCHEME})

_DECISION_STATUS = {AUTO: "evaluated", HOLD: "pending_approval", DENY: "denied"}


@dataclass
class GateResult:
    verdict: str                       # AUTO / HOLD / DENY / FREE（资源本身不收费，2xx）/ ERROR
    rule_id: str
    reason: str
    url: str
    quote: Optional[Quote] = None
    decision_id: Optional[int] = None
    approval_id: Optional[int] = None
    paid: bool = False
    tx_hash: Optional[str] = None
    explorer_url: Optional[str] = None
    data: Any = None                   # 资源内容（付款成功或免费时）
    error: Optional[str] = None
    state: Optional[str] = None        # payment_requests.state
    payment_id: Optional[int] = None
    request_id: Optional[str] = None
    replay: bool = False               # True：这是同一 request_id 的重复请求，返回的是已有结果
    risk: Optional[dict] = None
    extra: dict = field(default_factory=dict)


def fetch_paid(url: str, purpose: str, max_price_xrp: Optional[Decimal] = None, *,
               agent_id: str, request_id: str) -> GateResult:
    """max_price_xrp：调用方（agent）自己给的价格上限，超过直接 DENY，在策略之前判定。"""
    maintenance.sweep()
    row, created = payments.create_or_get(agent_id, request_id, url, purpose)
    if not created:
        return _replay(row, url)
    pid = row["id"]

    # 不跟随重定向：3xx 按错误处理，避免被带到别的地方去拿报价或内容
    try:
        probe = requests.get(url, headers=payment_executor.buyer_headers(agent_id), timeout=PROBE_TIMEOUT,
                             allow_redirects=False)
    except requests.Timeout:
        return _probe_failed(pid, url, purpose, f"请求资源超时（{PROBE_TIMEOUT} 秒无响应）")
    except requests.RequestException as exc:
        return _probe_failed(pid, url, purpose, f"连接资源失败：{type(exc).__name__}")
    if 200 <= probe.status_code < 300:
        sm.advance(pid, sm.RESOURCE_DELIVERED, verdict="FREE", rule_id="-", reason=f"HTTP {probe.status_code}，无需付款",
                   result_json=json.dumps(payment_executor.body(probe), ensure_ascii=False))
        return from_row(payments.get(pid))
    if probe.status_code != 402:
        return _probe_failed(pid, url, purpose, _http_error(probe))

    try:
        quote = parse_402(probe)
    except QuoteError as exc:
        # 报价都解析不出来，没法评估风险，直接拒绝
        return _deny(pid, url, purpose, None, Decision(DENY, "R0", f"402 报价无法解析：{exc}"), from_state=sm.RECEIVED)
    cfg = policy.load_policy()
    quote = _choose_method(quote, cfg)
    method = ESCROW_SCHEME if quote.scheme == ESCROW_SCHEME else "x402"
    sm.advance(pid, sm.QUOTED, quote_json=json.dumps(quote.requirements.to_dict()), quote_hash=quote.terms_hash(),
               amount_drops=quote.amount_drops, pay_to=quote.pay_to, method=method)

    decision = (_unsupported_scheme(quote) or _over_max_price(quote, max_price_xrp)
                or evaluation.decide(quote, cfg))
    if decision.verdict == AUTO and method == ESCROW_SCHEME:
        decision = Decision(HOLD, "ESCROW", "escrow 付款由金库锁款，需要人工审批和设备签名")
    decision_id = audit.log_decision(url, purpose, quote, decision, status=_DECISION_STATUS[decision.verdict])
    sm.advance(pid, sm.EVALUATED, verdict=decision.verdict, rule_id=decision.rule_id, reason=decision.reason,
               decision_id=decision_id)

    if decision.verdict == AUTO:
        try:
            budget.reserve(pid, quote.amount_drops, budget.AUTO_KIND, cfg, from_state=sm.EVALUATED)
        except BudgetExceeded as exc:
            # 预检查通过、但原子预留时额度已被并发请求占用：改判为预留失败给出的结果（R5 HOLD / R7 DENY）
            decision = exc.decision
            audit.update_decision(decision_id, decision, _DECISION_STATUS[decision.verdict])
        else:
            memo = {"verdict": AUTO, "rule_id": decision.rule_id, "payment_id": pid}
            row = payment_executor.execute(pid, agent_signer(), budget.AUTO_KIND, memo)
            return from_row(row)

    if decision.verdict == HOLD:
        return _hold(pid, url, purpose, quote, decision, decision_id, cfg)
    return _deny(pid, url, purpose, quote, decision, from_state=sm.EVALUATED, decision_id=decision_id, cfg=cfg)


def _probe_failed(pid: int, url: str, purpose: str, reason: str) -> GateResult:
    """探测阶段失败（非 2xx / 非 402、连接失败、超时）：不付款，不把响应内容交给 agent，审计记为失败。"""
    decision = Decision("ERROR", "PROBE", reason)
    decision_id = audit.log_decision(url, purpose, None, decision, status="fetch_failed")
    sm.advance(pid, sm.FAILED, verdict="ERROR", rule_id="PROBE", reason=reason, error=reason, decision_id=decision_id)
    return from_row(payments.get(pid))


def _http_error(resp: requests.Response) -> str:
    text = f"HTTP {resp.status_code}" + (f" {resp.reason}" if resp.reason else "")
    if 300 <= resp.status_code < 400:
        return f"{text}：资源要求重定向到 {resp.headers.get('Location', '（未给出地址）')[:120]}，不自动跟随"
    return f"{text}：{'商户端错误' if resp.status_code >= 500 else '请求被拒绝或资源不存在'}"


def _replay(row: dict, url: str) -> GateResult:
    if row["url"] != url:
        return GateResult("ERROR", "IDEMPOTENCY", f"request_id {row['request_id']} 已用于另一个 URL：{row['url']}",
                          url, state=row["state"], payment_id=row["id"], request_id=row["request_id"], replay=True)
    if row["state"] == sm.UNKNOWN_SETTLEMENT:
        row = reconcile(row["id"])
    result = from_row(row)
    result.replay = True
    return result


def _choose_method(quote: Quote, cfg: policy.PolicyConfig) -> Quote:
    """金额超过 escrow_threshold 且商户支持 escrow → 用 escrow（先验货再放款）；只提供 escrow 的商户也走 escrow。"""
    escrow_option = quote.with_scheme(ESCROW_SCHEME)
    if escrow_option and quote.amount_drops and quote.amount_drops > cfg.escrow_threshold_drops:
        return escrow_option
    return quote


def _unsupported_scheme(quote: Quote) -> Optional[Decision]:
    if quote.scheme in SUPPORTED_SCHEMES:
        return None
    return Decision(DENY, "R1", f"不支持的付款方式 {quote.scheme}（只支持 {', '.join(sorted(SUPPORTED_SCHEMES))}）")


def _over_max_price(quote: Quote, max_price_xrp: Optional[Decimal]) -> Optional[Decision]:
    if max_price_xrp is None or quote.amount_drops is None:
        return None
    limit_drops = int(Decimal(str(max_price_xrp)) * DROPS_PER_XRP)   # 向下取整：宁可少付
    if quote.amount_drops <= limit_drops:
        return None
    return Decision(DENY, "MAX_PRICE", f"报价 {fmt_xrp(quote.amount_drops)} XRP 超过调用方给的上限 {fmt_xrp(limit_drops)} XRP")


def _hold(pid: int, url: str, purpose: str, quote: Quote, decision: Decision, decision_id: int,
          cfg: policy.PolicyConfig) -> GateResult:
    risk = risk_explainer.explain(risk_facts(quote, decision, purpose, cfg), use_llm=True)
    risk_json = json.dumps(risk, ensure_ascii=False)
    approval_id = approvals.enqueue(quote, purpose, decision, decision_id, pid, cfg.approval_ttl_seconds, risk)
    audit.set_risk(decision_id, risk_json)
    sm.advance(pid, sm.PENDING_APPROVAL, verdict=decision.verdict, rule_id=decision.rule_id, reason=decision.reason,
               approval_id=approval_id, risk_json=risk_json)
    return from_row(payments.get(pid))


def _deny(pid: int, url: str, purpose: str, quote: Optional[Quote], decision: Decision, *, from_state: str,
          decision_id: Optional[int] = None, cfg: Optional[policy.PolicyConfig] = None) -> GateResult:
    """DENY 只用规则模板解释（确定、即时）；LLM 解释留给需要人看的 HOLD。"""
    if decision_id is None:
        decision_id = audit.log_decision(url, purpose, quote, decision, status="denied")
    risk = risk_explainer.explain(risk_facts(quote, decision, purpose, cfg or policy.load_policy()), use_llm=False)
    risk_json = json.dumps(risk, ensure_ascii=False)
    audit.set_risk(decision_id, risk_json)
    sm.advance(pid, sm.DENIED, verdict=decision.verdict, rule_id=decision.rule_id, reason=decision.reason,
               decision_id=decision_id, risk_json=risk_json)
    return from_row(payments.get(pid))


def risk_facts(quote: Optional[Quote], decision: Decision, purpose: str, cfg: policy.PolicyConfig) -> dict[str, Any]:
    """给风险解释器的结构化事实。只有数据，不含任何密钥。"""
    facts: dict[str, Any] = {
        "verdict": decision.verdict, "rule_id": decision.rule_id, "rule_reason": decision.reason,
        "purpose": purpose,
        "auto_committed_today_xrp": fmt_xrp(budget.committed_today(budget.AUTO_KIND)),
        "auto_daily_budget_xrp": fmt_xrp(cfg.daily_budget_drops),
        "auto_limit_per_payment_xrp": fmt_xrp(cfg.auto_limit_drops),
        "rate_window_seconds": cfg.rate_window_seconds,
    }
    if quote is not None:
        facts.update(
            url=quote.url, merchant_description=quote.resource.description if quote.resource else None,
            amount_xrp=fmt_xrp(quote.amount_drops) if quote.amount_drops else quote.amount,
            asset=quote.asset, network=quote.network, pay_to=quote.pay_to,
            payee_verified=evaluation.payee_verified(quote.pay_to, cfg) if quote.network == cfg.network else None,
            recent_payments_to_payee=payments.submitted_to_since(quote.pay_to, time.time() - cfg.rate_window_seconds),
        )
    return facts


def from_row(row: dict) -> GateResult:
    quote = Quote(row["url"], PaymentRequirements.from_dict(json.loads(row["quote_json"]))) if row.get("quote_json") else None
    tx_hash = row.get("tx_hash")
    return GateResult(
        verdict=row.get("verdict") or "-", rule_id=row.get("rule_id") or "-", reason=row.get("reason") or "",
        url=row["url"], quote=quote, decision_id=row.get("decision_id"), approval_id=row.get("approval_id"),
        paid=row["state"] in sm.PAID_STATES, tx_hash=tx_hash,
        explorer_url=explorer_tx_url(tx_hash) if tx_hash else None,
        data=payments.result(row), error=row.get("error"), state=row["state"], payment_id=row["id"],
        request_id=row["request_id"], risk=json.loads(row["risk_json"]) if row.get("risk_json") else None,
    )
