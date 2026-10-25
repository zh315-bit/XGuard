"""守卫自己提交到 XRPL 的交易（不经过 x402 商户）：补款、escrow 锁款 / 放款 / 退款、热钱包封顶。

- prepare()：autofill，并设置 LastLedgerSequence（留够人在设备上核对的时间）
- submit_signed()：提交已签名交易并等待验证；网络错误抛 SubmitUnknown（带交易哈希），
  调用方按哈希对账，绝不重发
"""

from dataclasses import replace
from typing import Any

from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import AccountInfo, Ledger
from xrpl.models.transactions import Transaction
from xrpl.transaction import autofill, submit_and_wait
from xrpl.wallet import Wallet

from guard.xrpl_client import get_client

LEDGERS_TO_SIGN = 150   # 约 10 分钟：人在设备上核对、签名的时间


class SubmitUnknown(RuntimeError):
    """交易已发出但没有拿到最终结果。"""

    def __init__(self, tx_hash: str, reason: str) -> None:
        super().__init__(reason)
        self.tx_hash = tx_hash


def prepare(tx: Transaction, ledgers: int = LEDGERS_TO_SIGN) -> Transaction:
    client = get_client()
    return autofill(replace(tx, last_ledger_sequence=get_latest_validated_ledger_sequence(client) + ledgers), client)


def submit_signed(blob: str) -> dict[str, Any]:
    """提交已签名的 blob，等待验证，返回 result（含 meta.TransactionResult）。"""
    tx = Transaction.from_blob(blob)
    try:
        return submit_and_wait(tx, get_client(), autofill=False, check_fee=False).result
    except Exception as exc:  # noqa: BLE001 —— 结果未知，交给按哈希对账
        raise SubmitUnknown(tx.get_hash(), f"No final result after submission: {exc}") from exc


def submit_with_wallet(tx: Transaction, wallet: Wallet) -> dict[str, Any]:
    """用服务器上的钱包（热钱包）签名并提交：escrow 放款 / 退款、热钱包封顶。"""
    return submit_and_wait(tx, get_client(), wallet).result


def result_code(result: dict[str, Any]) -> str:
    return result.get("meta", {}).get("TransactionResult", "unknown")


def ledger_close_time() -> int:
    """最新已验证账本的关闭时间（ripple 时间）。CancelAfter 按它判断，而不是本机时钟。"""
    return int(get_client().request(Ledger(ledger_index="validated")).result["ledger"]["close_time"])


def balance_drops(address: str) -> int:
    data = get_client().request(AccountInfo(account=address, ledger_index="validated")).result["account_data"]
    return int(data["Balance"])
