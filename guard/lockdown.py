"""安全锁定：出现安全事件时暂停一切付款，并把热钱包里的钱转回金库（Contain）。

- 锁定状态存在数据库（lockdown 表，单行），重启后保持
- 锁定期间：新的付款请求一律 DENY（规则 LOCKDOWN）；待审批的单子不能批准；已在签名中的付款提交前再查一次
- contain()：先锁定，再把热钱包余额（保留账户储备和手续费）转回金库。交易带审计 Memo，
  签名后、提交前把哈希登记到 contained_txs，哨兵据此把回收交易对账为 matched
- 没有任何代码路径会自动调用 contain()：只有控制台的人工操作（API）会触发
- 只能在控制台手动解除（release），解除记入审计
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from xrpl.models.requests import AccountInfo, ServerState
from xrpl.models.transactions import Payment

from guard import chain
from guard.db import connect, immediate
from guard.policy import DENY, Decision, fmt_xrp
from guard.xrpl_client import address_of, explorer_tx_url, get_client, load_wallet

RULE_ID = "LOCKDOWN"
REASON = "Security incident in progress; payments are paused"


class Locked(RuntimeError):
    pass


def decision() -> Decision:
    return Decision(DENY, RULE_ID, REASON)


def is_locked() -> bool:
    with connect() as conn:
        row = conn.execute("SELECT locked FROM lockdown WHERE id = 1").fetchone()
    return bool(row and row["locked"])


def ensure_unlocked() -> None:
    if is_locked():
        raise Locked(REASON)


def status() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM lockdown WHERE id = 1").fetchone()
        sweep = conn.execute("SELECT * FROM contained_txs WHERE kind = 'contain' ORDER BY created_at DESC LIMIT 1").fetchone()
    state = dict(row) if row else {"locked": 0, "locked_at": None, "reason": None, "released_at": None, "release_note": None}
    state["locked"] = bool(state["locked"])
    last = dict(sweep) if sweep else None
    if last:
        last["explorer_url"] = explorer_tx_url(last["tx_hash"])
        last["amount_xrp"] = fmt_xrp(last["amount_drops"])
    # 横幅只显示本次锁定期间的回收交易
    state["containment"] = last if last and state.get("locked_at") and last["created_at"] >= state["locked_at"] else None
    return state


def lock(reason: str) -> dict[str, Any]:
    now = time.time()
    with immediate() as conn:
        row = conn.execute("SELECT locked FROM lockdown WHERE id = 1").fetchone()
        if not (row and row["locked"]):
            conn.execute("INSERT INTO lockdown (id, locked, locked_at, reason) VALUES (1, 1, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET locked = 1, locked_at = excluded.locked_at, reason = excluded.reason, "
                         "released_at = NULL, release_note = NULL", (now, reason))
            _event(conn, now, "lockdown_engaged", None, {"reason": reason})
    return status()


def release(note: str) -> dict[str, Any]:
    """只由控制台调用。"""
    now = time.time()
    with immediate() as conn:
        row = conn.execute("SELECT locked FROM lockdown WHERE id = 1").fetchone()
        if not (row and row["locked"]):
            raise ValueError("The guard is not locked")
        conn.execute("UPDATE lockdown SET locked = 0, released_at = ?, release_note = ? WHERE id = 1", (now, note))
        _event(conn, now, "lockdown_released", None, {"note": note})
    return status()


def account_reserve_drops(address: str) -> int:
    """账户储备 = 基础储备 + 每个对象的增量储备 × OwnerCount（按当前已验证账本）。"""
    client = get_client()
    state = client.request(ServerState()).result["state"]["validated_ledger"]
    owner_count = client.request(AccountInfo(account=address, ledger_index="validated")).result["account_data"].get("OwnerCount", 0)
    return int(state["reserve_base"]) + int(state["reserve_inc"]) * int(owner_count)


def contain(note: str = "Contain from the console") -> dict[str, Any]:
    """先锁定，再把热钱包可动用的余额转回金库。没有可转的余额时只锁定。"""
    lock(note)
    agent = load_wallet("agent")
    balance = chain.balance_drops(agent.address)
    reserve = account_reserve_drops(agent.address)
    tx = chain.prepare(Payment(account=agent.address, destination=address_of("treasury"), amount="1"))
    fee = int(tx.fee)
    amount = balance - reserve - fee
    if amount <= 0:
        return {**status(), "swept": None}
    memo = {"action": "contain", "reason": REASON, "balance_before_xrp": fmt_xrp(balance),
            "reserve_xrp": fmt_xrp(reserve), "returned_xrp": fmt_xrp(amount)}
    from guard.hot_wallet import sweep_to_treasury   # 避免循环导入
    swept = sweep_to_treasury(amount, memo, kind="contain", memo_type="agentguard/contain", prepared=tx)
    return {**status(), "swept": swept}


def contained_tx(tx_hash: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM contained_txs WHERE tx_hash = ?", (tx_hash,)).fetchone()
    return dict(row) if row else None


def _event(conn, ts: float, kind: str, tx_hash: Optional[str], detail: dict) -> None:
    conn.execute("INSERT INTO audit_events (ts, created_at, kind, tx_hash, detail) VALUES (?, ?, ?, ?, ?)",
                 (ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"), kind, tx_hash,
                  json.dumps(detail, ensure_ascii=False)))
