"""所有 XRPL 连接都集中在这里。

切换到 Quicknode 只需改 .env 里的 XRPL_RPC_URL，代码不用动。
"""

from decimal import Decimal

from xrpl.account import get_balance
from xrpl.clients import JsonRpcClient
from xrpl.utils import drops_to_xrp
from xrpl.wallet import Wallet

from guard.config import env, require_env

DEFAULT_RPC_URL = "https://s.altnet.rippletest.net:51234/"
DEFAULT_EXPLORER_URL = "https://testnet.xrpl.org"


def rpc_url() -> str:
    return env("XRPL_RPC_URL", DEFAULT_RPC_URL)


def get_client() -> JsonRpcClient:
    return JsonRpcClient(rpc_url())


def load_wallet(role: str) -> Wallet:
    """按角色名（agent / treasury / ...）从 .env 读 seed 还原钱包。私钥只在守卫进程内存在。"""
    return Wallet.from_seed(require_env(f"{role.upper()}_SEED"))


def address_of(role: str) -> str:
    return require_env(f"{role.upper()}_ADDRESS")


def xrp_balance(address: str) -> Decimal:
    return drops_to_xrp(str(get_balance(address, get_client())))


def explorer_tx_url(tx_hash: str) -> str:
    return f"{env('XRPL_EXPLORER_URL', DEFAULT_EXPLORER_URL)}/transactions/{tx_hash}"
