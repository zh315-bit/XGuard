"""独立哨兵（watchdog/sentinel.py）的对账和存储，以及给控制台的视图。

哨兵是另一个进程，和守卫 API 共用 SQLite（WAL）。它只读守卫的表、只写 monitor_* / audit_events，
不参与也不改变任何付款流程。

对账规则（受监控账户 = 金库、热钱包）：
- 受监控账户发起的交易（outgoing）必须能在守卫数据库里找到对应的已授权记录：
    payment_requests.tx_hash      x402 付款 / 补款 / escrow 锁款（签名后、提交前就写入）
    escrows.create/finish/cancel  escrow 锁款 / 放款 / 退款
    EscrowFinish / EscrowCancel   Owner + OfferSequence 对得上守卫建的 escrow（放款 / 退款的哈希提交后才写库）
    热钱包 → 金库的 XRP 付款       余额封顶（cap_hot_wallet.py），钱回到金库
    decisions.tx_hash             审计里记过的付款
  找不到 → UNAUTHORIZED：写审计事件、告警。宽限期内先记为 pending，避免和“刚上链、还没写库”撞车。
- 别人发起、转入受监控账户的交易（incoming）只记录。
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from guard.config import env
from guard.db import connect
from guard.xrpl_client import address_of, explorer_tx_url

MATCHED, UNAUTHORIZED, INCOMING, PENDING = "matched", "UNAUTHORIZED", "incoming", "pending"
HEARTBEAT_STALE_SECONDS = 30

_PAYMENT_KIND = {"x402": "x402", "topup": "topup", "escrow": "escrow-lock"}


def watched_accounts() -> dict[str, str]:
    """地址 → 角色。"""
    return {address_of("treasury"): "treasury", address_of("agent"): "hot_wallet"}


# ---- 对账 ----

def match(tx: dict[str, Any], watched: dict[str, str]) -> Optional[tuple[str, str]]:
    """outgoing 交易在守卫数据库里的授权记录：(match_kind, match_ref)，没有返回 None。"""
    tx_hash = tx["hash"]
    with connect() as conn:
        row = conn.execute("SELECT kind FROM contained_txs WHERE tx_hash = ?", (tx_hash,)).fetchone()
        if row:
            ref = "demo reset sweep to treasury" if row["kind"] == "demo-refill" else "lockdown sweep to treasury"
            return row["kind"], ref
        row = conn.execute("SELECT id, method FROM payment_requests WHERE tx_hash = ?", (tx_hash,)).fetchone()
        if row:
            return _PAYMENT_KIND.get(row["method"], row["method"]), f"payment #{row['id']}"
        for col, kind in (("create_tx", "escrow-lock"), ("finish_tx", "escrow-release"), ("cancel_tx", "escrow-refund")):
            row = conn.execute(f"SELECT payment_request_id FROM escrows WHERE {col} = ?", (tx_hash,)).fetchone()
            if row:
                return kind, f"payment #{row['payment_request_id']}"
        if tx.get("tx_type") in ("EscrowFinish", "EscrowCancel") and tx.get("offer_sequence") is not None:
            row = conn.execute("SELECT payment_request_id FROM escrows WHERE owner = ? AND offer_sequence = ?",
                               (tx.get("owner"), tx["offer_sequence"])).fetchone()
            if row:
                kind = "escrow-release" if tx["tx_type"] == "EscrowFinish" else "escrow-refund"
                return kind, f"payment #{row['payment_request_id']}"
        row = conn.execute("SELECT id FROM decisions WHERE tx_hash = ?", (tx_hash,)).fetchone()
        if row:
            return "audit", f"decision #{row['id']}"
    if (tx.get("tx_type") == "Payment" and watched.get(tx.get("account")) == "hot_wallet"
            and watched.get(tx.get("destination")) == "treasury" and (tx.get("amount") or "").endswith(" XRP")):
        return "hot-wallet-cap", "returned to treasury"
    return None


def record(tx: dict[str, Any], watched: dict[str, str], source: str, now: Optional[float] = None) -> Optional[dict]:
    """记一笔交易并初步对账。已经记过（实时订阅和补查重叠）返回 None。"""
    now = time.time() if now is None else now
    roles = sorted({watched[a] for a in (tx.get("account"), tx.get("destination"), tx.get("owner")) if a in watched})
    outgoing = tx.get("account") in watched
    found = match(tx, watched) if outgoing else None
    status = INCOMING if not outgoing else MATCHED if found else PENDING
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO monitor_txs (hash, ledger_index, close_time, seen_at, source, account, destination,
                   tx_type, amount, result, owner, offer_sequence, watched, direction, status, match_kind, match_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tx["hash"], tx.get("ledger_index"), tx.get("close_time"), now, source, tx.get("account"),
             tx.get("destination"), tx.get("tx_type"), tx.get("amount"), tx.get("result"), tx.get("owner"),
             tx.get("offer_sequence"), ",".join(roles),
             "outgoing" if outgoing else "incoming", status,
             found[0] if found else None, found[1] if found else None))
        if cur.rowcount == 0:
            return None
    return get_tx(tx["hash"])


def resolve_pending(watched: dict[str, str], grace_seconds: float, now: Optional[float] = None) -> list[dict]:
    """宽限期已过的 pending：再对一次账，仍找不到授权记录 → UNAUTHORIZED。返回新标记的交易。"""
    now = time.time() if now is None else now
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM monitor_txs WHERE status = ? AND seen_at <= ?",
                                              (PENDING, now - grace_seconds))]
    flagged = []
    for row in rows:
        found = match(row, watched)
        with connect() as conn:
            if found:
                conn.execute("UPDATE monitor_txs SET status = ?, match_kind = ?, match_ref = ? WHERE hash = ? AND status = ?",
                             (MATCHED, found[0], found[1], row["hash"], PENDING))
                continue
            cur = conn.execute("UPDATE monitor_txs SET status = ?, flagged_at = ? WHERE hash = ? AND status = ?",
                               (UNAUTHORIZED, now, row["hash"], PENDING))
            if cur.rowcount:
                detail = {k: row[k] for k in ("account", "destination", "tx_type", "amount", "result", "ledger_index", "source")}
                _event(conn, now, "unauthorized_tx", row["hash"], detail)
        if not found:
            flagged.append(get_tx(row["hash"]))
    return flagged


def get_tx(tx_hash: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM monitor_txs WHERE hash = ?", (tx_hash,)).fetchone()
    return dict(row) if row else None


# ---- 连接状态（哨兵写，API 读）----

def set_state(**values: Any) -> None:
    now = time.time()
    with connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO monitor_state (key, value, updated_at) VALUES (?, ?, ?)",
                         [(k, None if v is None else str(v), now) for k, v in values.items()])


def get_state() -> dict[str, Optional[str]]:
    with connect() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM monitor_state")}


def cursor() -> Optional[int]:
    value = get_state().get("cursor_ledger")
    return int(value) if value else None


def advance_cursor(ledger_index: int) -> None:
    """断线重连后从这个账本（含）开始补查。只前进不后退。"""
    current = cursor()
    if current is None or ledger_index > current:
        set_state(cursor_ledger=ledger_index)


# ---- 控制台 ----

def health(now: Optional[float] = None) -> dict[str, Any]:
    """connected：连着且最近有心跳；stale：进程没在更新（退出或卡住）；其余照实返回 connecting / disconnected。"""
    now = time.time() if now is None else now
    state = get_state()
    beat = float(state["heartbeat"]) if state.get("heartbeat") else None
    status = state.get("status") or "offline"
    if beat is None:
        status = "offline"
    elif now - beat > HEARTBEAT_STALE_SECONDS:
        status = "stale"
    return {"status": status, "heartbeat_age_seconds": None if beat is None else round(now - beat, 1),
            "endpoint": state.get("endpoint"), "cursor_ledger": int(state["cursor_ledger"]) if state.get("cursor_ledger") else None,
            "reconnects": int(state.get("reconnects") or 0), "last_error": state.get("last_error"),
            "connected_since": float(state["connected_since"]) if state.get("connected_since") else None,
            "last_backfill": json.loads(state["last_backfill"]) if state.get("last_backfill") else None,
            "grace_seconds": grace_seconds()}


def grace_seconds() -> float:
    return float(env("SENTINEL_GRACE_SECONDS", "2"))


def view(limit: int = 20) -> dict[str, Any]:
    with connect() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM monitor_txs GROUP BY status")}
        recent = [dict(r) for r in conn.execute("SELECT * FROM monitor_txs ORDER BY COALESCE(ledger_index, 0) DESC, seen_at DESC LIMIT ?", (limit,))]
        alerts = [dict(r) for r in conn.execute(
            "SELECT * FROM monitor_txs WHERE status = ? AND acknowledged_at IS NULL ORDER BY flagged_at DESC", (UNAUTHORIZED,))]
    watched = watched_accounts()
    for row in (*recent, *alerts):
        row["explorer_url"] = explorer_tx_url(row["hash"])
        row["account_role"] = watched.get(row["account"])
        row["destination_role"] = watched.get(row["destination"])
    return {"health": health(), "counts": {"matched": counts.get(MATCHED, 0), "unauthorized": counts.get(UNAUTHORIZED, 0),
                                           "incoming": counts.get(INCOMING, 0), "pending": counts.get(PENDING, 0)},
            "recent": recent, "open_alerts": alerts,
            "watched": [{"address": a, "role": r} for a, r in watched.items()]}


def acknowledge(tx_hash: str, note: str) -> Optional[dict]:
    """人工“标记为已处理”。交易本身仍是 UNAUTHORIZED，只是不再显示警报。"""
    now = time.time()
    with connect() as conn:
        cur = conn.execute("UPDATE monitor_txs SET acknowledged_at = ?, ack_note = ? WHERE hash = ? AND status = ? "
                           "AND acknowledged_at IS NULL", (now, note, tx_hash, UNAUTHORIZED))
        if cur.rowcount:
            _event(conn, now, "alert_acknowledged", tx_hash, {"note": note})
    return get_tx(tx_hash)


def list_events(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))]


def _event(conn, ts: float, kind: str, tx_hash: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit_events (ts, created_at, kind, tx_hash, detail) VALUES (?, ?, ?, ?, ?)",
                 (ts, datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"), kind, tx_hash,
                  json.dumps(detail, ensure_ascii=False)))
