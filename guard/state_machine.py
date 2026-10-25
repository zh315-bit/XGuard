"""支付请求的状态机。所有状态变化都必须走 advance()，只能按 TRANSITIONS 里的合法路径转移。

主路径：
  RECEIVED → QUOTED → EVALUATED → {DENIED | PENDING_APPROVAL | RESERVED}
  PENDING_APPROVAL →（人工批准）→ RESERVED
  RESERVED → SIGNING → SUBMITTED → VALIDATED → RESOURCE_DELIVERED
异常：EXPIRED / REJECTED / FAILED / UNKNOWN_SETTLEMENT

escrow（先验货再放款）在 SUBMITTED 之后走另一条路：
  SUBMITTED → ESCROW_LOCKED（EscrowCreate 已上链，钱锁住但没付出）→ DELIVERY_RECEIVED（拿到货）
            → RESOURCE_DELIVERED（验货通过，EscrowFinish 已上链）
            → REFUND_PENDING（验货不通过 / 没交货，等 CancelAfter）→ REFUNDED（EscrowCancel 已上链）

两条不在主路径上的边，单独说明：
- RECEIVED → RESOURCE_DELIVERED：资源本身不收费（不是 402），无需付款
- RECEIVED → DENIED：402 报价无法解析（R0），没有报价可进入 QUOTED
- RESERVED / SIGNING → DENIED：付款前重新拿报价，发现被调包（R8）或重新判定为 DENY

advance() 是 compare-and-set：UPDATE ... WHERE state IN (合法前驱)。
所以并发时只有一个调用者能完成同一次转移，另一个会得到 IllegalTransition。
"""

import sqlite3
import time
from typing import Any, Optional

from guard.db import connect

RECEIVED = "RECEIVED"
QUOTED = "QUOTED"
EVALUATED = "EVALUATED"
DENIED = "DENIED"
PENDING_APPROVAL = "PENDING_APPROVAL"
RESERVED = "RESERVED"
SIGNING = "SIGNING"
SUBMITTED = "SUBMITTED"
VALIDATED = "VALIDATED"
RESOURCE_DELIVERED = "RESOURCE_DELIVERED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"
FAILED = "FAILED"
UNKNOWN_SETTLEMENT = "UNKNOWN_SETTLEMENT"
ESCROW_LOCKED = "ESCROW_LOCKED"
DELIVERY_RECEIVED = "DELIVERY_RECEIVED"
REFUND_PENDING = "REFUND_PENDING"
REFUNDED = "REFUNDED"

TRANSITIONS: dict[str, frozenset[str]] = {
    RECEIVED: frozenset({QUOTED, DENIED, FAILED, RESOURCE_DELIVERED}),
    QUOTED: frozenset({EVALUATED, FAILED}),
    EVALUATED: frozenset({DENIED, PENDING_APPROVAL, RESERVED}),
    PENDING_APPROVAL: frozenset({RESERVED, REJECTED, EXPIRED}),
    RESERVED: frozenset({SIGNING, DENIED, EXPIRED, FAILED}),
    # SIGNING 阶段还没有任何签名交易发出，所以失败可以直接 FAILED
    SIGNING: frozenset({SUBMITTED, DENIED, EXPIRED, FAILED}),
    # 一旦发出，失败只能先进 UNKNOWN_SETTLEMENT，由对账决定 VALIDATED 还是 FAILED
    SUBMITTED: frozenset({VALIDATED, UNKNOWN_SETTLEMENT, ESCROW_LOCKED}),
    UNKNOWN_SETTLEMENT: frozenset({VALIDATED, FAILED, ESCROW_LOCKED}),
    VALIDATED: frozenset({RESOURCE_DELIVERED}),
    ESCROW_LOCKED: frozenset({DELIVERY_RECEIVED, REFUND_PENDING}),
    DELIVERY_RECEIVED: frozenset({RESOURCE_DELIVERED, REFUND_PENDING}),
    REFUND_PENDING: frozenset({REFUNDED}),
}
TERMINAL = frozenset({DENIED, REJECTED, EXPIRED, FAILED, RESOURCE_DELIVERED, REFUNDED})
# 钱已经付出去的状态
PAID_STATES = frozenset({VALIDATED, RESOURCE_DELIVERED})
# 已签名交易可能在路上：在这些状态下绝不能重新签名/重发
IN_FLIGHT = frozenset({SUBMITTED, UNKNOWN_SETTLEMENT})
# 钱锁在 escrow 里：既没付出也没退回
LOCKED_STATES = frozenset({ESCROW_LOCKED, DELIVERY_RECEIVED, REFUND_PENDING})

_UPDATABLE = {"verdict", "rule_id", "reason", "quote_json", "quote_hash", "amount_drops", "pay_to",
              "decision_id", "approval_id", "tx_hash", "last_ledger_sequence", "submitted_at",
              "result_json", "error", "risk_json", "method"}


class IllegalTransition(RuntimeError):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())


def advance(payment_id: int, to: str, conn: Optional[sqlite3.Connection] = None, **fields: Any) -> None:
    """把 payment_id 转到 to，同时写入 fields。不是合法前驱状态时抛 IllegalTransition。"""
    sources = [s for s, targets in TRANSITIONS.items() if to in targets]
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"不能通过状态转移修改字段：{unknown}")
    cols = "".join(f", {k} = ?" for k in fields)
    sql = (f"UPDATE payment_requests SET state = ?, updated_at = ?{cols} "
           f"WHERE id = ? AND state IN ({','.join('?' * len(sources))})")
    params = (to, time.time(), *fields.values(), payment_id, *sources)

    if conn is not None:
        rowcount = conn.execute(sql, params).rowcount
    else:
        with connect() as c:
            rowcount = c.execute(sql, params).rowcount
    if rowcount != 1:
        current = _current_state(payment_id, conn)
        raise IllegalTransition(f"支付请求 #{payment_id} 不能从 {current} 转到 {to}")


def _current_state(payment_id: int, conn: Optional[sqlite3.Connection]) -> Optional[str]:
    c = conn or connect()
    row = c.execute("SELECT state FROM payment_requests WHERE id = ?", (payment_id,)).fetchone()
    return row["state"] if row else None
