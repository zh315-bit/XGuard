"""金库与热钱包的链上状态（余额、Regular Key、主密钥是否禁用），给控制台的 Treasury 页面用。

控制台每 2 秒轮询，这里缓存 5 秒，避免每次都打 RPC。
"""

import time
from typing import Any

from xrpl.models.requests import AccountInfo, AccountTx

from guard.config import env
from guard.xrpl_client import address_of, explorer_tx_url, get_client

LSF_DISABLE_MASTER = 0x00100000
EXPLORER_ACCOUNT = "https://testnet.xrpl.org/accounts/{}"
CACHE_SECONDS = 5

_cache: dict[str, tuple[float, dict]] = {}
_disable_tx: dict[str, str] = {}    # 禁用主密钥的交易哈希：一旦找到就不会变，永久缓存
ASF_DISABLE_MASTER = 4


def disable_master_tx(address: str) -> str | None:
    """在账户历史里找 AccountSet SetFlag=asfDisableMaster 且成功的那笔交易。"""
    if address in _disable_tx:
        return _disable_tx[address]
    try:
        txs = get_client().request(AccountTx(account=address, limit=200)).result.get("transactions", [])
    except Exception:  # noqa: BLE001 —— 展示用
        return None
    for t in txs:
        tx = t.get("tx_json") or t.get("tx") or {}
        if (tx.get("TransactionType") == "AccountSet" and tx.get("SetFlag") == ASF_DISABLE_MASTER
                and t.get("meta", {}).get("TransactionResult") == "tesSUCCESS"):
            _disable_tx[address] = t.get("hash") or tx.get("hash")
            return _disable_tx[address]
    return None


def account(address: str) -> dict[str, Any]:
    cached = _cache.get(address)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        data = get_client().request(AccountInfo(account=address, ledger_index="validated")).result["account_data"]
        info = {
            "address": address, "balance_xrp": int(data["Balance"]) / 1_000_000,
            "regular_key": data.get("RegularKey"),
            "master_disabled": bool(data.get("Flags", 0) & LSF_DISABLE_MASTER),
            "explorer_url": EXPLORER_ACCOUNT.format(address), "error": None,
        }
    except Exception as exc:  # noqa: BLE001 —— 展示用，查不到就显示错误
        info = {"address": address, "error": str(exc), "explorer_url": EXPLORER_ACCOUNT.format(address)}
    _cache[address] = (time.time(), info)
    return info


def status() -> dict[str, Any]:
    treasury = account(address_of("treasury"))
    device = env("TREASURY_SIGNER_ADDRESS", "")
    from guard import policy
    cfg = policy.load_policy()
    hot = account(address_of("agent"))
    cap = cfg.hot_wallet_max_balance_drops / 1_000_000
    disable_tx = disable_master_tx(treasury["address"]) if treasury.get("master_disabled") else None
    return {
        "security": {
            "master_disabled": bool(treasury.get("master_disabled")),
            "disable_master_tx": disable_tx,
            "disable_master_tx_url": explorer_tx_url(disable_tx) if disable_tx else None,
            "regular_key": treasury.get("regular_key"),
            "regular_key_url": EXPLORER_ACCOUNT.format(treasury["regular_key"]) if treasury.get("regular_key") else None,
            "regular_key_is_device": bool(device) and treasury.get("regular_key") == device,
            "hot_wallet_cap_xrp": cap,
            "hot_wallet_balance_xrp": hot.get("balance_xrp"),
            "hot_wallet_url": hot["explorer_url"],
            "treasury_url": treasury["explorer_url"],
        },
        "hot_wallet": {**hot, "cap_xrp": cap,
                       "within_cap": hot.get("balance_xrp") is not None and hot["balance_xrp"] <= cap},
        "treasury": treasury,
        "signer_mode": env("SIGNER_MODE", "software"),
        "device_address": device or None,
        "treasury_controlled_by_device": bool(device) and treasury.get("regular_key") == device,
    }
