"""把策略引擎需要的上下文（预算、频率、链上凭证）查好，再调用 policy.evaluate。"""

import time
from typing import Optional

from guard import budget, credentials, payments, policy
from guard.policy import Decision, EvalContext
from guard.quote import Quote


def decide(quote: Quote, cfg: Optional[policy.PolicyConfig] = None) -> Decision:
    """每次重新读 policy.yaml，现场改阈值立即生效。"""
    cfg = cfg or policy.load_policy()
    ctx = EvalContext(
        auto_committed_today_drops=budget.committed_today(budget.AUTO_KIND),
        recent_payments_to_payee=payments.submitted_to_since(quote.pay_to, time.time() - cfg.rate_window_seconds),
        payee_verified=payee_verified(quote.pay_to, cfg),
    )
    return policy.evaluate(quote, ctx, cfg)


def payee_verified(pay_to: str, cfg: policy.PolicyConfig) -> Optional[bool]:
    """查询失败返回 None，R3 会判 HOLD——绝不因为查不到就放行。"""
    try:
        return credentials.is_verified(pay_to, cfg.credential_issuer, cfg.credential_type)
    except credentials.CredentialLookupError:
        return None
