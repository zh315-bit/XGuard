"""策略引擎：报价 → AUTO / HOLD / DENY。

规则按 RULES 的顺序判定，命中即停。原则：明显恶意的直接拒，不确定的交给人。
本模块是纯函数：需要的历史数据（今日已付、近期次数）由调用方通过 EvalContext 传入。
所有金额比较都用整数 drops（1 XRP = 1,000,000 drops），XRP 小数只出现在给人看的文字里。
"""

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

import yaml

from guard.config import ROOT, env
from guard.quote import Quote

DROPS_PER_XRP = 1_000_000

AUTO, HOLD, DENY = "AUTO", "HOLD", "DENY"


@dataclass(frozen=True)
class Decision:
    verdict: str
    rule_id: str
    reason: str


@dataclass(frozen=True)
class PolicyConfig:
    network: str
    allowed_assets: frozenset[str]
    hard_cap_drops: int
    auto_limit_drops: int
    daily_budget_drops: int           # 自动付款（AUTO）的日预算，R5
    daily_total_cap_drops: int        # 含人工批准在内的每日总支出上限，人工批准也不能突破
    quote_ttl_seconds: int            # 自动付款预算预留的有效期
    approval_ttl_seconds: int         # 审批单有效期（已按 DEMO_MODE / 环境变量解析）
    hot_wallet_max_balance_drops: int
    escrow_threshold_drops: int
    escrow_cancel_after_seconds: int
    rate_window_seconds: int
    rate_max_requests: int
    credential_issuer: str
    credential_type: str
    blacklist: frozenset[str]


@dataclass(frozen=True)
class EvalContext:
    auto_committed_today_drops: int   # 今日自动付款已花费 + 已预留
    recent_payments_to_payee: int     # 窗口期内对同一收款地址真正发起过付款的次数（不含本次）
    payee_verified: Optional[bool]    # 链上凭证是否有效；None = 查询失败（按不可信处理）


def load_policy(path: Optional[Path] = None) -> PolicyConfig:
    """默认读项目根目录的 policy.yaml；POLICY_PATH 可指向别的文件（测试用）。"""
    path = path or Path(env("POLICY_PATH", str(ROOT / "policy.yaml")))
    raw = yaml.safe_load(path.read_text())
    rate = raw.get("rate_limit", {})
    return PolicyConfig(
        network=raw["network"],
        allowed_assets=frozenset(raw["allowed_assets"]),
        hard_cap_drops=xrp_to_drops(raw["hard_cap_xrp"]),
        auto_limit_drops=xrp_to_drops(raw["auto_limit_xrp"]),
        daily_budget_drops=xrp_to_drops(raw["daily_budget_xrp"]),
        daily_total_cap_drops=xrp_to_drops(raw["daily_total_cap_xrp"]),
        quote_ttl_seconds=int(raw["quote_ttl_seconds"]),
        approval_ttl_seconds=_approval_ttl(raw),
        hot_wallet_max_balance_drops=xrp_to_drops(raw.get("hot_wallet_max_balance_xrp", 20)),
        escrow_threshold_drops=xrp_to_drops(raw.get("escrow_threshold_xrp", 5)),
        escrow_cancel_after_seconds=int(raw.get("escrow_cancel_after_seconds_demo", 75)
                                        if env("DEMO_MODE", "false").lower() == "true"
                                        else raw.get("escrow_cancel_after_seconds", 600)),
        rate_window_seconds=int(rate.get("window_seconds", 60)),
        rate_max_requests=int(rate.get("max_requests", 5)),
        credential_issuer=_expand_required(raw["merchant_credential"]["issuer"]),
        credential_type=str(raw["merchant_credential"]["type"]),
        blacklist=_address_set(raw.get("blacklist")),
    )


def _approval_ttl(raw: dict) -> int:
    """优先级：环境变量 APPROVAL_TTL_SECONDS > DEMO_MODE 下的 approval_ttl_seconds_demo（默认 300）> approval_ttl_seconds。"""
    override = env("APPROVAL_TTL_SECONDS", "")
    if override:
        return int(override)
    if env("DEMO_MODE", "false").lower() == "true":
        return int(raw.get("approval_ttl_seconds_demo", 300))
    return int(raw.get("approval_ttl_seconds", raw["quote_ttl_seconds"]))


def xrp_to_drops(value: object) -> int:
    """配置里的 XRP 数（如 0.5）精确换算成整数 drops；不足 1 drop 的小数直接报错。"""
    drops = Decimal(str(value)) * DROPS_PER_XRP
    if drops != drops.to_integral_value():
        raise ValueError(f"{value} XRP is not a whole number of drops")
    return int(drops)


def fmt_xrp(drops: int) -> str:
    """drops → 给人看的 XRP 字符串，如 8000000 → '8'，10000 → '0.01'。"""
    return format((Decimal(drops) / DROPS_PER_XRP).normalize(), "f")


def _address_set(entries: Optional[list]) -> frozenset[str]:
    """展开 ${VAR}；展开失败（仍含 $）或为空的条目丢弃，避免误放行。"""
    expanded = (os.path.expandvars(str(e)).strip() for e in entries or [])
    return frozenset(a for a in expanded if a and "$" not in a)


def _expand_required(value: str) -> str:
    expanded = os.path.expandvars(str(value)).strip()
    if not expanded or "$" in expanded:
        raise ValueError(f"{value} in policy.yaml cannot be expanded; check .env")
    return expanded


# ---- 规则：每条返回 Decision（命中）或 None（放行到下一条） ----

Rule = Callable[[Quote, EvalContext, PolicyConfig], Optional[Decision]]


def r1_network_asset(q: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Optional[Decision]:
    if q.network != cfg.network:
        return Decision(DENY, "R1", f"Network {q.network} is not {cfg.network}")
    if q.asset not in cfg.allowed_assets:
        return Decision(DENY, "R1", f"Asset {q.asset} is not in the allowed list {sorted(cfg.allowed_assets)}")
    return None


def r7_hard_cap(q: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Optional[Decision]:
    return check_hard_cap(q.amount_drops, cfg)


def check_hard_cap(amount_drops: int, cfg: PolicyConfig) -> Optional[Decision]:
    """R7 单独导出：人工批准时也要再查一次。"""
    if amount_drops > cfg.hard_cap_drops:
        return Decision(DENY, "R7", f"Amount {fmt_xrp(amount_drops)} XRP exceeds the hard cap of {fmt_xrp(cfg.hard_cap_drops)} XRP")
    return None


def r2_blacklist(q: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Optional[Decision]:
    if q.pay_to in cfg.blacklist:
        return Decision(DENY, "R2", f"Payee {q.pay_to} is on the blocklist")
    return None


def r3_credential(q: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Optional[Decision]:
    if ctx.payee_verified is None:
        return Decision(HOLD, "R3", f"Could not look up the on-chain credential of {q.pay_to}; treated as unverified")
    if not ctx.payee_verified:
        return Decision(HOLD, "R3", f"Payee {q.pay_to} has no valid {cfg.credential_type} credential")
    return None


def r4_auto_limit(q: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Optional[Decision]:
    if q.amount_drops > cfg.auto_limit_drops:
        return Decision(HOLD, "R4", f"Amount {fmt_xrp(q.amount_drops)} XRP exceeds the per-payment auto limit of {fmt_xrp(cfg.auto_limit_drops)} XRP")
    return None


def r5_daily_budget(q: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Optional[Decision]:
    """预检查。真正的预算扣减在 budget.reserve() 的 BEGIN IMMEDIATE 事务里再判一次。"""
    return auto_budget_decision(ctx.auto_committed_today_drops, q.amount_drops, cfg)


def auto_budget_decision(committed_drops: int, amount_drops: int, cfg: PolicyConfig) -> Optional[Decision]:
    if committed_drops + amount_drops > cfg.daily_budget_drops:
        return Decision(HOLD, "R5", f"Auto payments spent/reserved today {fmt_xrp(committed_drops)} + this payment {fmt_xrp(amount_drops)} "
                                    f"exceeds the daily budget of {fmt_xrp(cfg.daily_budget_drops)} XRP")
    return None


def r6_rate_limit(q: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Optional[Decision]:
    if ctx.recent_payments_to_payee >= cfg.rate_max_requests:
        return Decision(HOLD, "R6", f"{ctx.recent_payments_to_payee} payments to this merchant in the last "
                                    f"{cfg.rate_window_seconds} s, reaching the limit of {cfg.rate_max_requests}")
    return None


# R1 必须第一个：后面的规则依赖 amount_drops，只有原生 XRP 报价才有值
RULES: list[Rule] = [r1_network_asset, r7_hard_cap, r2_blacklist, r3_credential,
                     r4_auto_limit, r5_daily_budget, r6_rate_limit]


def evaluate(quote: Quote, ctx: EvalContext, cfg: PolicyConfig) -> Decision:
    for rule in RULES:
        decision = rule(quote, ctx, cfg)
        if decision is not None:
            return decision
    return Decision(AUTO, "DEFAULT", "No rule matched; paid automatically")
