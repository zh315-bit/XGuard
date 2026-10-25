"""热钱包余额封顶：把超过 hot_wallet_max_balance_xrp（policy.yaml，默认 20 XRP）的部分转回金库。

转账由热钱包自己签名（它的私钥本来就在服务器上），带审计 Memo agentguard/hot-wallet。
反方向（金库给热钱包补款）不在这里：只能在控制台发起补款审批，经人工批准 + Ledger 签名。

运行：.venv/bin/python scripts/cap_hot_wallet.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guard import hot_wallet  # noqa: E402
from guard.xrpl_client import address_of  # noqa: E402

EXPLORER_ACCOUNT = "https://testnet.xrpl.org/accounts/{}"


def main() -> None:
    before = hot_wallet.status()
    print(f"热钱包 {address_of('agent')}：{before['balance_xrp']} XRP，上限 {before['cap_xrp']} XRP")
    result = hot_wallet.cap_excess()
    if result is None:
        print("没有超出上限，不需要转账。")
    else:
        print(f"转回金库 {result['returned_xrp']} XRP：{result['result']}  {result['explorer_url']}")
        print(f"热钱包余额：{result['balance_after_xrp']} XRP")
        if result["result"] != "tesSUCCESS":
            sys.exit(1)
    print(f"\n热钱包  {EXPLORER_ACCOUNT.format(address_of('agent'))}")
    print(f"金库    {EXPLORER_ACCOUNT.format(address_of('treasury'))}")


if __name__ == "__main__":
    main()
