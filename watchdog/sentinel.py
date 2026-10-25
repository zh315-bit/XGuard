"""独立监控哨兵：通过 Quicknode 的 WSS 端点实时订阅金库和热钱包，把每笔已验证交易和守卫数据库对账。

和守卫 API 分开跑（独立进程）：守卫进程被攻破、私钥被直接拿去转账时，钱不经过守卫的任何代码，
只有链上能看到。哨兵只读守卫的表，只写 monitor_* / audit_events，不参与付款。

- 受监控账户转出、守卫没有对应已授权记录 → UNAUTHORIZED：写审计事件、告警（日志 + 可选 webhook）、控制台红色警报
- 转入（退款、补款、别人付款）只记录
- 断线自动重连（指数退避）；重连后先订阅、再用 account_tx 从上次处理到的账本补查，避免漏报
- 连续 STALE_SECONDS 收不到新账本也当作断线（连接半死不活时 websocket 不一定报错）

对账规则见 guard/monitor.py。

运行：.venv/bin/python watchdog/sentinel.py（start_all.sh 会启动它）
配置：XRPL_WSS_URL（Quicknode 端点的 wss:// 地址）、SENTINEL_GRACE_SECONDS、SENTINEL_ALERT_WEBHOOK_URL（可选）
"""

import asyncio
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from xrpl.asyncio.clients import AsyncWebsocketClient  # noqa: E402
from xrpl.models.requests import AccountTx, Subscribe  # noqa: E402
from xrpl.utils import ripple_time_to_posix  # noqa: E402

from guard import monitor  # noqa: E402
from guard.config import env  # noqa: E402
from guard.policy import fmt_xrp  # noqa: E402

DEFAULT_WSS_URL = "wss://s.altnet.rippletest.net:51233/"
STALE_SECONDS = 20       # 账本约 3–4 秒一个，20 秒没有新账本就重连
TICK_SECONDS = 0.5
MAX_BACKOFF = 30
BACKFILL_PAGE = 200

log = logging.getLogger("sentinel")


def wss_url() -> str:
    return env("XRPL_WSS_URL", DEFAULT_WSS_URL)


def endpoint_label(url: str) -> str:
    """只显示主机名：Quicknode 的 token 在路径里，不能写进库或日志。"""
    host = urlparse(url).hostname or url
    return f"{host} (Quicknode)" if host.endswith("quiknode.pro") else host


def normalize(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把订阅推送 / account_tx 条目（API v1 或 v2）整理成对账用的扁平字段。未验证的返回 None。"""
    if not msg.get("validated"):
        return None
    tx = msg.get("tx_json") or msg.get("transaction") or msg.get("tx") or {}
    tx_hash = msg.get("hash") or tx.get("hash")
    if not tx_hash:
        return None
    meta = msg.get("meta") or msg.get("metaData") or {}
    if isinstance(meta, str):        # 二进制 meta：不需要细节，结果码拿不到就算了
        meta = {}
    amount = meta.get("delivered_amount") or tx.get("DeliverMax") or tx.get("Amount")
    close_time = None
    if msg.get("close_time_iso"):
        close_time = datetime.fromisoformat(msg["close_time_iso"].replace("Z", "+00:00")).timestamp()
    elif tx.get("date") is not None:
        close_time = float(ripple_time_to_posix(tx["date"]))
    return {"hash": tx_hash, "ledger_index": msg.get("ledger_index") or tx.get("ledger_index"),
            "close_time": close_time, "account": tx.get("Account"), "destination": tx.get("Destination"),
            "tx_type": tx.get("TransactionType"), "amount": format_amount(amount),
            "result": meta.get("TransactionResult") or msg.get("engine_result"),
            "owner": tx.get("Owner"), "offer_sequence": tx.get("OfferSequence")}


def format_amount(amount: Any) -> Optional[str]:
    if amount is None or amount == "unavailable":
        return None
    if isinstance(amount, str):
        return f"{fmt_xrp(int(amount))} XRP"
    if isinstance(amount, dict) and "value" in amount:
        return f"{amount['value']} {amount.get('currency', '')}"
    return json.dumps(amount)


class Sentinel:
    def __init__(self, url: Optional[str] = None,
                 connect: Callable[[str], Any] = AsyncWebsocketClient,
                 watched: Optional[dict[str, str]] = None,
                 grace_seconds: Optional[float] = None,
                 tick_seconds: float = TICK_SECONDS,
                 retry_base: float = 1.0,
                 alert_hook: Optional[Callable[[dict], None]] = None) -> None:
        self.url = url or wss_url()
        self.connect = connect
        self.watched = watched or monitor.watched_accounts()
        self.grace = monitor.grace_seconds() if grace_seconds is None else grace_seconds
        self.tick = tick_seconds
        self.retry_base = retry_base
        self.alert_hook = alert_hook or webhook_alert
        self.stopping = asyncio.Event()
        self.reconnects = 0

    def stop(self) -> None:
        self.stopping.set()

    async def run(self) -> None:
        """重连循环：每次会话断开后按 1, 2, 4 … 30 秒退避再连。"""
        backoff = self.retry_base
        monitor.set_state(status="connecting", endpoint=endpoint_label(self.url), reconnects=self.reconnects)
        while not self.stopping.is_set():
            started = time.time()
            try:
                async with self.connect(self.url) as client:
                    await self.session(client)
            except Exception as exc:  # noqa: BLE001 —— 任何错误都按断线处理
                if self.stopping.is_set():
                    break
                log.warning("连接断开：%s", exc)
                monitor.set_state(status="disconnected", last_error=f"{type(exc).__name__}: {exc}"[:300])
            if self.stopping.is_set():
                break
            if time.time() - started > MAX_BACKOFF:     # 稳定连过一段时间，退避重新计算
                backoff = self.retry_base
            self.reconnects += 1
            monitor.set_state(status="reconnecting", reconnects=self.reconnects)
            try:
                await asyncio.wait_for(self.stopping.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF)
        monitor.set_state(status="stopped")

    async def session(self, client: Any) -> None:
        sub = await client.request(Subscribe(accounts=list(self.watched), streams=["ledger"]))
        if not sub.is_successful():
            raise ConnectionError(f"subscribe failed: {sub.result}")
        validated = sub.result.get("ledger_index")
        now = time.time()
        monitor.set_state(status="connected", connected_since=now, heartbeat=now, endpoint=endpoint_label(self.url))
        log.info("已订阅 %s，验证账本 %s", ", ".join(f"{r}={a}" for a, r in self.watched.items()), validated)

        # 先订阅再补查：补查结束到实时推送之间没有空档，重叠的部分按哈希去重
        start = monitor.cursor()
        if start is None:
            monitor.advance_cursor(validated)
        else:
            await self.backfill(client, start)

        queue: asyncio.Queue = asyncio.Queue()
        pump = asyncio.create_task(self._pump(client, queue))
        last_ledger_at = time.time()
        try:
            while not self.stopping.is_set():
                try:
                    msg = await asyncio.wait_for(queue.get(), self.tick)
                except asyncio.TimeoutError:
                    msg = None
                if msg is _CLOSED:
                    raise ConnectionError("websocket closed")
                if msg is not None:
                    if msg.get("type") == "ledgerClosed":
                        last_ledger_at = time.time()
                        monitor.set_state(heartbeat=last_ledger_at, ledger=msg.get("ledger_index"))
                        monitor.advance_cursor(msg["ledger_index"])
                    elif msg.get("type") == "transaction":
                        self.handle(msg, "stream")
                self.resolve()
                if time.time() - last_ledger_at > STALE_SECONDS:
                    raise ConnectionError(f"no new ledger for {STALE_SECONDS} s")
        finally:
            pump.cancel()

    async def _pump(self, client: Any, queue: asyncio.Queue) -> None:
        try:
            async for msg in client:
                await queue.put(msg)
        finally:
            await queue.put(_CLOSED)

    async def backfill(self, client: Any, start: int) -> int:
        """从 start（含）到最新已验证账本，逐个账户 account_tx 补查。返回新发现的交易数。"""
        found = 0
        for address in self.watched:
            marker = None
            while True:
                resp = await client.request(AccountTx(account=address, ledger_index_min=start, ledger_index_max=-1,
                                                      forward=True, limit=BACKFILL_PAGE, marker=marker))
                if not resp.is_successful():
                    raise ConnectionError(f"account_tx failed: {resp.result}")
                for entry in resp.result.get("transactions", []):
                    found += self.handle(entry, "backfill")
                marker = resp.result.get("marker")
                if not marker:
                    break
        monitor.set_state(last_backfill=json.dumps({"from_ledger": start, "found": found, "ts": time.time()}))
        if found:
            log.warning("补查到断线期间的 %d 笔交易（账本 %s 起）", found, start)
        self.resolve()
        return found

    def handle(self, msg: dict[str, Any], source: str) -> int:
        tx = normalize(msg)
        if tx is None:
            return 0
        row = monitor.record(tx, self.watched, source)
        if row is None:
            return 0
        if row["ledger_index"]:
            monitor.advance_cursor(row["ledger_index"])
        log.info("%s %s %s %s → %s %s", source, row["tx_type"], row["hash"][:12], row["amount"] or "",
                 row["status"], row["match_kind"] or "")
        return 1

    def resolve(self) -> None:
        for row in monitor.resolve_pending(self.watched, self.grace):
            log.error("UNAUTHORIZED %s %s %s → %s  %s", row["tx_type"], row["amount"], row["account"],
                      row["destination"], row["hash"])
            try:
                self.alert_hook(row)
            except Exception:  # noqa: BLE001 —— 告警通道失败不影响记录（审计事件已写）
                log.exception("告警发送失败")


_CLOSED = object()


def webhook_alert(row: dict) -> None:
    """可选的外部告警：SENTINEL_ALERT_WEBHOOK_URL 收到一条 JSON。生产环境应换成独立部署的告警服务。"""
    url = env("SENTINEL_ALERT_WEBHOOK_URL", "")
    if not url:
        return
    payload = {"event": "unauthorized_tx", **{k: row[k] for k in ("hash", "account", "destination", "tx_type", "amount",
                                                                  "result", "ledger_index")}}

    def send() -> None:     # 在线程里发，不阻塞订阅循环
        try:
            requests.post(url, json=payload, timeout=5).raise_for_status()
        except requests.RequestException as exc:
            log.error("告警 webhook 失败：%s", exc)
    threading.Thread(target=send, daemon=True).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sentinel = Sentinel()
    log.info("哨兵启动：%s", endpoint_label(sentinel.url))

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, sentinel.stop)
        await sentinel.run()

    asyncio.run(run())


if __name__ == "__main__":
    main()
