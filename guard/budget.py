"""预算预留。判定 AUTO 或人工批准时，在同一个 BEGIN IMMEDIATE 事务里“检查额度 + 写入预留”，
并发的两笔请求只能串行通过这一步，合计超预算时后到的那笔一定会被拦下。

一笔预留的生命周期：reserved →（付款上链）spent，或 →（失败 / 过期 / 被拒）released。
日额度 = 当天 reserved + spent 的总和。全部是整数 drops。
"""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from guard.db import connect, immediate
from guard.policy import PolicyConfig, auto_budget_decision, check_hard_cap, fmt_xrp, Decision, HOLD
from guard import state_machine as sm

AUTO_KIND, APPROVED_KIND = "auto", "approved"


class BudgetExceeded(Exception):
    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


def reserve(payment_id: int, amount_drops: int, kind: str, cfg: PolicyConfig, from_state: str) -> None:
    """原子地检查额度并预留，同时把支付请求从 from_state 转到 RESERVED。超额抛 BudgetExceeded。

    - 所有付款：硬上限 R7、每日总上限（自动 + 人工）
    - 自动付款另外受 R5 自动日预算限制
    """
    with immediate() as conn:
        hard = check_hard_cap(amount_drops, cfg)
        if hard:
            raise BudgetExceeded(hard)
        day = _today()
        total = _committed(conn, day)
        if total + amount_drops > cfg.daily_total_cap_drops:
            raise BudgetExceeded(Decision(HOLD, "R5", (
                f"Total spent/reserved today {fmt_xrp(total)} + this payment {fmt_xrp(amount_drops)} "
                f"exceeds the daily total cap of {fmt_xrp(cfg.daily_total_cap_drops)} XRP")))
        if kind == AUTO_KIND:
            over = auto_budget_decision(_committed(conn, day, AUTO_KIND), amount_drops, cfg)
            if over:
                raise BudgetExceeded(over)
        now = time.time()
        # 人工批准后可能还要在硬件设备上签名，预留按审批有效期计算，避免签名途中被提前释放
        ttl = cfg.approval_ttl_seconds if kind == APPROVED_KIND else cfg.quote_ttl_seconds
        conn.execute(
            """INSERT INTO budget_reservations (payment_request_id, day, kind, amount_drops, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, 'reserved', ?, ?)""",
            (payment_id, day, kind, amount_drops, now, now + ttl))
        if from_state != sm.RESERVED:
            sm.advance(payment_id, sm.RESERVED, conn=conn)


def spend(payment_id: int) -> None:
    _set_status(payment_id, "spent", ("reserved",))


def release(payment_id: int) -> None:
    _set_status(payment_id, "released", ("reserved",))


def committed_today(kind: Optional[str] = None) -> int:
    with connect() as conn:
        return _committed(conn, _today(), kind)


def get(payment_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM budget_reservations WHERE payment_request_id = ?", (payment_id,)).fetchone()
    return dict(row) if row else None


def expired_reservations(now: Optional[float] = None) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM budget_reservations WHERE status = 'reserved' AND expires_at < ?",
                            (now or time.time(),)).fetchall()
    return [dict(r) for r in rows]


def detail_today() -> dict:
    """今日按状态拆分：已花费 / 预留中（drops 与 XRP）。"""
    with connect() as conn:
        rows = conn.execute("SELECT status, kind, COALESCE(SUM(amount_drops), 0) AS s FROM budget_reservations "
                            "WHERE day = ? GROUP BY status, kind", (_today(),)).fetchall()
    out = {f"{r['kind']}_{r['status']}_xrp": fmt_xrp(r["s"]) for r in rows}
    spent = sum(r["s"] for r in rows if r["status"] == "spent")
    reserved = sum(r["s"] for r in rows if r["status"] == "reserved")
    return {**out, "spent_xrp": fmt_xrp(spent), "reserved_xrp": fmt_xrp(reserved)}


def summary(cfg: PolicyConfig) -> dict:
    auto = committed_today(AUTO_KIND)
    total = committed_today()
    return {
        "auto_daily_budget_xrp": fmt_xrp(cfg.daily_budget_drops),
        "auto_committed_today_xrp": fmt_xrp(auto),
        "auto_remaining_today_xrp": fmt_xrp(max(cfg.daily_budget_drops - auto, 0)),
        "daily_total_cap_xrp": fmt_xrp(cfg.daily_total_cap_drops),
        "total_committed_today_xrp": fmt_xrp(total),
        "auto_limit_per_payment_xrp": fmt_xrp(cfg.auto_limit_drops),
        "hard_cap_per_payment_xrp": fmt_xrp(cfg.hard_cap_drops),
    }


def _committed(conn: sqlite3.Connection, day: str, kind: Optional[str] = None) -> int:
    sql = "SELECT COALESCE(SUM(amount_drops), 0) AS s FROM budget_reservations WHERE day = ? AND status IN ('reserved', 'spent')"
    params: tuple = (day,)
    if kind:
        sql += " AND kind = ?"
        params += (kind,)
    return int(conn.execute(sql, params).fetchone()["s"])


def _set_status(payment_id: int, status: str, from_statuses: tuple[str, ...]) -> None:
    with connect() as conn:
        conn.execute(f"UPDATE budget_reservations SET status = ? WHERE payment_request_id = ? "
                     f"AND status IN ({','.join('?' * len(from_statuses))})", (status, payment_id, *from_statuses))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
