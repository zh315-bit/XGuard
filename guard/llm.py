"""守卫里所有 LLM 调用的统一入口：固定超时 + 有限次数重试，并记录每个来源最近一次的结果（控制台的 LLM 状态灯用）。

- 用流式调用：LLM_TIMEOUT_SECONDS（默认 30 秒）是“连续这么久没有收到任何数据”才算超时，用来发现断连，
  而不是限制模型生成整段结果的时间（非流式时 30 秒要等整个 JSON 生成完，API 一慢就会连续超时）；
  另有总时长上限 LLM_TOTAL_SECONDS（默认 90 秒），超过就放弃，不重试。
- 只对暂时性错误重试（超时、网络中断、429 限速、5xx / 529 过载），
  最多重试 LLM_MAX_RETRIES 次（默认 2 次），间隔 1 秒、2 秒；鉴权失败、额度不足、请求错误不重试。
- 最终失败抛 LLMUnavailable，带人能看懂的原因；调用方自己决定怎么保守处理（风险解释改用模板、验货不放款）。
"""

import logging
import time
from typing import Any, Optional

from guard.config import env
from guard.db import connect

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = float(env("LLM_TIMEOUT_SECONDS", "30"))
MAX_RETRIES = int(env("LLM_MAX_RETRIES", "2"))
TOTAL_SECONDS = float(env("LLM_TOTAL_SECONDS", "90"))
BACKOFF_SECONDS = (1, 2, 4)

KIND_TEXT = {
    "timeout": "request timed out", "network": "network connection dropped", "rate_limit": "rate limited (429)",
    "overloaded": "Anthropic overloaded (529)", "server": "Anthropic server error (5xx)",
    "auth": "invalid API key or no permission", "quota": "insufficient account credit", "bad_request": "request rejected (400)",
    "too_slow": "generation too slow, over the total time limit", "refusal": "model refused", "not_configured": "ANTHROPIC_API_KEY not configured", "other": "unknown error",
}


class LLMUnavailable(Exception):
    def __init__(self, kind: str, detail: str, attempts: int) -> None:
        self.kind, self.detail, self.attempts = kind, detail, attempts
        retried = f", retried {attempts - 1} time(s)" if attempts > 1 else ""
        super().__init__(f"{KIND_TEXT.get(kind, kind)}{retried}")


def classify(exc: BaseException) -> tuple[str, bool]:
    """(错误类型, 是否值得重试)。"""
    import anthropic
    import httpx
    if isinstance(exc, anthropic.APITimeoutError) or isinstance(exc, httpx.TimeoutException):
        return "timeout", True
    if isinstance(exc, anthropic.APIConnectionError) or isinstance(exc, httpx.TransportError):
        return "network", True
    if isinstance(exc, anthropic.RateLimitError) or (isinstance(exc, anthropic.APIStatusError) and exc.status_code == 429):
        return "rate_limit", True
    if isinstance(exc, anthropic.APIStatusError):
        status, text = exc.status_code, str(exc).lower()
        if status == 529 or "overloaded" in text:
            return "overloaded", True
        if status >= 500:
            return "server", True
        if status in (401, 403):
            return "auth", False
        if "credit" in text or "billing" in text or status == 402:
            return "quota", False
        return "bad_request", False
    return "other", False


class _TooSlow(Exception):
    pass


def create_message(source: str, **kwargs: Any) -> Any:
    """等价于 client.beta.messages.create(**kwargs)（返回完整消息），内部用流式调用，带超时、总时长上限和重试。
    source 用于状态记录（risk_explainer / delivery_verifier）。"""
    api_key = env("ANTHROPIC_API_KEY", "")
    if not api_key:
        record(source, False, "not_configured", "")
        raise LLMUnavailable("not_configured", "", 0)
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)   # 重试由这里统一控制
    attempts, started = 0, time.monotonic()
    while True:
        attempts += 1
        try:
            response = _stream_to_message(client, started, kwargs)
        except _TooSlow as exc:
            record(source, False, "too_slow", str(exc))
            raise LLMUnavailable("too_slow", str(exc), attempts) from exc
        except Exception as exc:  # noqa: BLE001
            kind, retryable = classify(exc)
            if retryable and attempts <= MAX_RETRIES and time.monotonic() - started < TOTAL_SECONDS:
                log.warning("%s LLM 调用失败（%s：%s），第 %d 次重试", source, kind, exc, attempts)
                time.sleep(BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)])
                continue
            record(source, False, kind, str(exc)[:300])
            raise LLMUnavailable(kind, str(exc)[:300], attempts) from exc
        record(source, True)
        return response


def _stream_to_message(client: Any, started: float, kwargs: dict[str, Any]) -> Any:
    with client.beta.messages.stream(**kwargs) as stream:
        for _ in stream:
            if time.monotonic() - started > TOTAL_SECONDS:
                raise _TooSlow(f"still not finished after {TOTAL_SECONDS:.0f} s")
        return stream.get_final_message()


# ---- 状态记录：每个来源（风险解释、验货、agent）最近一次调用的结果 ----

def record(source: str, ok: bool, kind: Optional[str] = None, detail: Optional[str] = None) -> None:
    try:
        with connect() as conn:
            conn.execute(
                """INSERT INTO llm_status (source, ts, ok, kind, detail) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET ts = excluded.ts, ok = excluded.ok,
                                                     kind = excluded.kind, detail = excluded.detail""",
                (source, time.time(), int(ok), kind, detail))
    except Exception as exc:  # noqa: BLE001 —— 状态记录失败不能影响调用本身
        log.warning("记录 LLM 状态失败：%s", exc)


def recent() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM llm_status ORDER BY ts DESC").fetchall()
    return [{**dict(r), "ok": bool(r["ok"]), "kind_text": KIND_TEXT.get(r["kind"]) if r["kind"] else None} for r in rows]


_probe_cache: dict[str, Any] = {}
PROBE_TTL_SECONDS = 30


def probe() -> dict[str, Any]:
    """主动探测（GET /v1/models，不消耗 token），30 秒缓存：网络、鉴权是否正常。"""
    now = time.time()
    if _probe_cache and now - _probe_cache["ts"] < PROBE_TTL_SECONDS:
        return _probe_cache["result"]
    api_key = env("ANTHROPIC_API_KEY", "")
    if not api_key:
        result = {"ok": False, "kind": "not_configured"}
    else:
        import anthropic
        started = time.time()
        try:
            anthropic.Anthropic(api_key=api_key, timeout=10, max_retries=0).models.list(limit=1)
            result = {"ok": True, "latency_ms": int((time.time() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "kind": classify(exc)[0], "detail": str(exc)[:300]}
    result["kind_text"] = KIND_TEXT.get(result.get("kind")) if result.get("kind") else None
    _probe_cache.update(ts=now, result=result)
    return result


def status() -> dict[str, Any]:
    """控制台状态灯：探测失败，或最近 10 分钟内有来源以失败结束（之后没有成功），都算不可用。"""
    p = probe()
    calls = recent()
    failing = [c for c in calls if not c["ok"] and time.time() - c["ts"] < 600]
    ok = p["ok"] and not failing
    if not p["ok"]:
        reason = p["kind_text"]
    elif failing:
        reason = f"Latest {_SOURCE_TEXT.get(failing[0]['source'], failing[0]['source'])} call failed: {failing[0]['kind_text']}"
    else:
        reason = None
    return {"ok": ok, "reason": reason, "probe": p, "calls": calls}


_SOURCE_TEXT = {"risk_explainer": "risk explainer", "delivery_verifier": "delivery verifier", "agent": "agent"}
