"""签名抽象。守卫只依赖 Signer 接口，不关心私钥在哪。

- SoftwareSigner：私钥在本进程（.env 里的 seed），立即签名
- ExternalSigner：私钥在 Ledger（现场用 Speculos 模拟器）上。sign() 把未签名交易登记成签名请求
  （signing_requests 表），然后等前端 / CLI 用设备签好并提交 blob；提交时已做完全部一致性校验
金库用哪个由 .env 的 SIGNER_MODE=speculos|software 决定；热钱包始终是 SoftwareSigner。

调用约定（所有签名器一致）：
- 调用方构造好交易并 autofill，SigningPubKey 设为 signer.public_key（Ledger 流程要求后端先填好）
- sign() 返回签好的十六进制 blob；调用方必须用 verify_signed_blob 核对外部返回的 blob
  没被篡改（外部签名器理论上可以签任何东西）
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from xrpl.core.binarycodec import decode, encode, encode_for_signing
from xrpl.core.keypairs import is_valid_message
from xrpl.models.transactions import Transaction
from xrpl.transaction import sign as xrpl_sign
from xrpl.wallet import Wallet

from guard import signing_requests
from guard.config import env, require_env
from guard.xrpl_client import address_of, load_wallet

# 签名产生/改写的字段，比对交易内容时排除
_SIGNATURE_FIELDS = {"TxnSignature", "Signers"}


class Signer(ABC):
    @property
    @abstractmethod
    def address(self) -> str: ...

    @property
    @abstractmethod
    def public_key(self) -> str: ...

    @abstractmethod
    async def sign(self, tx: Transaction) -> str:
        """签名并返回 blob（hex）。外部签名器可以在这里等待人工操作。"""


class SoftwareSigner(Signer):
    """私钥在内存里的签名器。只在守卫的审批服务进程里构造，不会进 MCP / agent。"""

    def __init__(self, wallet: Wallet) -> None:
        self._wallet = wallet

    @classmethod
    def from_env(cls, role: str) -> "SoftwareSigner":
        return cls(load_wallet(role))

    @property
    def address(self) -> str:
        return self._wallet.address

    @property
    def public_key(self) -> str:
        return self._wallet.public_key

    async def sign(self, tx: Transaction) -> str:
        return xrpl_sign(tx, self._wallet).blob()

    def __repr__(self) -> str:  # 防止日志里意外打印出 wallet（含 seed）
        return f"SoftwareSigner({self.address})"


class SigningFailed(RuntimeError):
    pass


class ExternalSigner(Signer):
    """签名在外部设备上完成。一个实例对应一张审批单的一次签名。"""

    POLL_SECONDS = 0.5

    def __init__(self, account: str, public_key: str, approval_id: int, payment_id: int, timeout_seconds: float) -> None:
        self._account, self._public_key = account, public_key
        self.approval_id, self.payment_id = approval_id, payment_id
        self.timeout_seconds = timeout_seconds

    @property
    def address(self) -> str:
        return self._account

    @property
    def public_key(self) -> str:
        return self._public_key

    async def sign(self, tx: Transaction) -> str:
        if tx.account != self._account or tx.signing_pub_key != self._public_key:
            raise SigningFailed("待签交易的 Account / SigningPubKey 不是金库 / 设备")
        if tx.last_ledger_sequence is None:
            raise SigningFailed("待签交易必须带 LastLedgerSequence")
        request_id = signing_requests.create(self.approval_id, self.payment_id, self._account, self._public_key,
                                             encode(tx.to_xrpl()), self.timeout_seconds)
        deadline = time.time() + self.timeout_seconds
        while True:
            req = signing_requests.get(request_id)
            if req["status"] == signing_requests.SIGNED:
                return req["signed_blob"]
            if req["status"] in (signing_requests.REJECTED, signing_requests.EXPIRED):
                raise SigningFailed(req.get("error") or f"签名请求 {req['status']}")
            if time.time() > deadline:
                signing_requests._finish(request_id, signing_requests.EXPIRED, error="等待设备签名超时")
                raise SigningFailed("等待设备签名超时")
            await asyncio.sleep(self.POLL_SECONDS)


def signer_mode() -> str:
    return env("SIGNER_MODE", "software").lower()


def approval_signer(approval_id: int, payment_id: int, timeout_seconds: float) -> Signer:
    """审批付款用的金库签名器：SIGNER_MODE=speculos 走设备，否则用服务器上的金库 seed。"""
    if signer_mode() == "speculos":
        return ExternalSigner(address_of("treasury"), require_env("TREASURY_SIGNER_PUBLIC_KEY"),
                              approval_id, payment_id, timeout_seconds)
    return treasury_signer()


def agent_signer() -> Signer:
    """AUTO 小额付款用的热钱包签名器。只在守卫进程里构造。"""
    return SoftwareSigner.from_env("agent")


def treasury_signer() -> Signer:
    """备用：服务器上的金库主密钥（金库禁用主密钥之后就不能用了）。"""
    return SoftwareSigner.from_env("treasury")


def verify_signed_blob(blob: str, expected: Transaction, signer: Signer) -> None:
    """确认签好的 blob 就是我们要签的那笔交易，且签名能用该签名器的公钥验证。"""
    signed = decode(blob)
    if signed.get("SigningPubKey") != signer.public_key:
        raise ValueError("签名公钥与签名器不一致")
    if _content(signed) != _content(expected.to_xrpl()):
        raise ValueError("签名后的交易内容与待签交易不一致")
    unsigned = {k: v for k, v in signed.items() if k not in _SIGNATURE_FIELDS}
    if not is_valid_message(bytes.fromhex(encode_for_signing(unsigned)), bytes.fromhex(signed.get("TxnSignature", "")),
                            signer.public_key):
        raise ValueError("签名验证不通过")


def _content(tx_json: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in tx_json.items() if k not in _SIGNATURE_FIELDS | {"SigningPubKey"}}
