"""商户凭证（XLS-70 Credentials）查询，供 R3 使用。

字段名按 xrpl-py 5.2.0 实测：
  LedgerEntry(credential=Credential(subject=..., issuer=..., credential_type=<hex>))
凭证必须同时满足：存在、已被 subject 接受（lsfAccepted）、未过期。
查询结果缓存 60 秒；网络错误不缓存，抛 CredentialLookupError，由调用方判 HOLD。
"""

import time
from datetime import datetime, timezone
from typing import Any, Optional

from xrpl.models.requests import LedgerEntry
from xrpl.models.requests.ledger_entry import Credential
from xrpl.utils import datetime_to_ripple_time, str_to_hex

from guard.xrpl_client import get_client

LSF_ACCEPTED = 0x00010000   # Credential 对象的 lsfAccepted（xrpl-py 没有导出这个常量）
CACHE_TTL_SECONDS = 60

# (subject, issuer, type) -> (查询时间, 是否有效)
_cache: dict[tuple[str, str, str], tuple[float, bool]] = {}


class CredentialLookupError(RuntimeError):
    """无法确定凭证状态（网络错误、节点异常）。绝不能当作“有效”处理。"""


def is_verified(subject: str, issuer: str, credential_type: str) -> bool:
    key = (subject, issuer, credential_type)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    node = fetch_credential(subject, issuer, credential_type)
    valid = node is not None and _is_active(node)
    _cache[key] = (time.monotonic(), valid)
    return valid


def fetch_credential(subject: str, issuer: str, credential_type: str) -> Optional[dict[str, Any]]:
    """返回已验证账本里的 Credential 对象；不存在返回 None；其他失败抛 CredentialLookupError。"""
    request = LedgerEntry(
        credential=Credential(subject=subject, issuer=issuer, credential_type=str_to_hex(credential_type)),
        ledger_index="validated",
    )
    try:
        response = get_client().request(request)
    except Exception as exc:  # httpx 等网络异常类型不固定
        raise CredentialLookupError(f"Network error during credential lookup: {exc}") from exc
    if response.is_successful():
        return response.result["node"]
    if response.result.get("error") == "entryNotFound":
        return None
    raise CredentialLookupError(f"Credential lookup failed: {response.result.get('error')}")


def clear_cache() -> None:
    _cache.clear()


def _is_active(node: dict[str, Any]) -> bool:
    if not node.get("Flags", 0) & LSF_ACCEPTED:
        return False
    expiration = node.get("Expiration")
    return expiration is None or expiration > datetime_to_ripple_time(datetime.now(timezone.utc))
