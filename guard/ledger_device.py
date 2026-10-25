"""Python 脚本访问 Ledger（Speculos）的入口：调用 frontend/cli/ledger.ts（官方 DMK + speculos 传输 + signer-kit-xrp）。

设备上需要人工确认的操作（签名）会阻塞，直到在设备屏幕上确认或拒绝；
设备的提示信息直接打印到当前终端（stderr 透传）。
"""

import json
import subprocess
from typing import Any

from xrpl.core.binarycodec import decode, encode, encode_for_signing
from xrpl.core.keypairs import is_valid_message

from guard.config import ROOT, env

FRONTEND = ROOT / "frontend"


class DeviceError(RuntimeError):
    pass


def _cli(*args: str, timeout: float) -> dict[str, Any]:
    proc = subprocess.run(["npx", "tsx", "cli/ledger.ts", *args], cwd=FRONTEND, stdout=subprocess.PIPE,
                          stderr=None, text=True, timeout=timeout,
                          env=_env())
    if proc.returncode != 0:
        raise DeviceError(_diagnose())
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _diagnose() -> str:
    """设备操作失败时看一眼 Speculos 屏幕，给出能照着做的提示。"""
    import requests
    base = env("SPECULOS_URL", "http://127.0.0.1:5001")
    try:
        texts = [e["text"] for e in requests.get(f"{base}/events", params={"currentscreenonly": "true"},
                                                 timeout=3).json()["events"]]
    except Exception:  # noqa: BLE001
        return f"Cannot reach Speculos ({base}): run scripts/start_speculos.sh first"
    if "Review transaction" in texts or any(" of " in t for t in texts) or "Hold to sign" in texts:
        return ("The device is stuck on an unfinished signing review (usually a previous script was interrupted while waiting). "
                "The device handles one request at a time: press Reject → Yes, reject on the device (or docker restart speculos-xrp), then retry")
    return f"Device operation failed (see the [ledger] output above); current screen: {' | '.join(texts) or 'empty'}"


def _env() -> dict[str, str]:
    import os
    return {**os.environ, "SPECULOS_URL": env("SPECULOS_URL", "http://127.0.0.1:5001"),
            "SPECULOS_MODEL": env("SPECULOS_MODEL", "STAX")}


def device_identity(timeout: float = 60) -> tuple[str, str]:
    """返回设备在 44'/144'/0'/0/0 上的 (XRP 地址, 公钥大写 hex)。"""
    out = _cli("address", timeout=timeout)
    return out["address"], out["publicKey"]


def sign_unsigned(unsigned_hex: str, timeout: float = 300) -> str:
    """把未签名交易交给设备签名，返回 DER 签名的大写 hex。"""
    return _cli("sign", unsigned_hex, timeout=timeout)["signature"]


def attach_signature(unsigned_hex: str, signature_hex: str) -> str:
    """把设备签名放进 TxnSignature，返回已签名的 blob；同时校验签名确实对应 SigningPubKey。"""
    tx = decode(unsigned_hex)
    if not is_valid_message(bytes.fromhex(encode_for_signing(tx)), bytes.fromhex(signature_hex), tx["SigningPubKey"]):
        raise DeviceError("The signature returned by the device failed verification")
    return encode({**tx, "TxnSignature": signature_hex})
