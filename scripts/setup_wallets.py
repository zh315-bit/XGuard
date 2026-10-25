"""用 testnet 水龙头创建并充值 6 个演示账户，写入 .env 和 .env.example。

运行：.venv/bin/python scripts/setup_wallets.py [--force]
已有 .env 时默认拒绝覆盖（避免丢失已充值账户的 seed），加 --force 才重建。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet, generate_faucet_wallet

from guard.xrpl_client import DEFAULT_EXPLORER_URL, DEFAULT_RPC_URL, xrp_balance

ROOT = Path(__file__).resolve().parent.parent
ROLES = ["treasury", "agent", "issuer", "merchant_a", "merchant_b", "merchant_c"]

# 非钱包类配置，同时写进 .env 和 .env.example
BASE_SETTINGS = {
    "XRPL_RPC_URL": DEFAULT_RPC_URL,
    "XRPL_EXPLORER_URL": DEFAULT_EXPLORER_URL,
    "X402_FACILITATOR_URL": "https://xrpl-facilitator-testnet.t54.ai",
    "X402_NETWORK": "xrpl:1",
    "MERCHANT_BASE_URL": "http://127.0.0.1:8000",
    "GUARD_DB_PATH": "data/agentguard.db",
    "GUARD_API_URL": "http://127.0.0.1:8100",
    "AGENT_MODEL_PROVIDER": "anthropic",
    "AGENT_MODEL_ID": "claude-opus-5-5",
    "ANTHROPIC_API_KEY": "",
}


def fund_wallet(client: JsonRpcClient, role: str, attempts: int = 3) -> Wallet:
    """水龙头偶尔限流或超时，失败时退避重试。"""
    for i in range(1, attempts + 1):
        try:
            return generate_faucet_wallet(client, usage_context="agentguard-hackathon")
        except Exception as exc:  # 水龙头错误类型不固定，统一重试
            print(f"  {role} 第 {i} 次充值失败：{exc}")
            time.sleep(5 * i)
    raise RuntimeError(f"{role} 充值失败，稍后重试")


def render_env(wallets: dict[str, Wallet], include_seeds: bool) -> str:
    lines = [f"{k}={v}" for k, v in BASE_SETTINGS.items()]
    for role, wallet in wallets.items():
        key = role.upper()
        lines.append("")
        lines.append(f"{key}_ADDRESS={wallet.address if include_seeds else ''}")
        lines.append(f"{key}_SEED={wallet.seed if include_seeds else ''}")
    return "\n".join(lines) + "\n"


def main() -> None:
    env_path = ROOT / ".env"
    if env_path.exists() and "--force" not in sys.argv:
        sys.exit(".env 已存在；确认要重建全部账户请加 --force")

    client = JsonRpcClient(BASE_SETTINGS["XRPL_RPC_URL"])
    wallets: dict[str, Wallet] = {}
    for role in ROLES:
        print(f"创建并充值 {role} ...")
        wallets[role] = fund_wallet(client, role)
        print(f"  {wallets[role].address}  余额 {xrp_balance(wallets[role].address)} XRP")

    env_path.write_text(render_env(wallets, include_seeds=True))
    (ROOT / ".env.example").write_text(render_env(wallets, include_seeds=False))
    print(f"\n已写入 {env_path} 和 .env.example")


if __name__ == "__main__":
    main()
