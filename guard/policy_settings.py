"""控制台的策略设置：读出主要金额阈值，校验后写回 policy.yaml，每次修改记入 policy_changes 审计表。

只改 policy.yaml 里已有的几行，保留其余内容和注释。判定每次都会重新读取 policy.yaml，写回后立即生效。
"""

import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import yaml

from guard import policy
from guard.config import ROOT, env
from guard.db import connect

# yaml 键 → 给人看的名字和说明
FIELDS: dict[str, tuple[str, str]] = {
    "auto_limit_xrp": ("Per-payment auto limit", "Payments up to this amount that pass every other check are paid automatically from the hot wallet"),
    "daily_budget_xrp": ("Daily auto budget", "Once automatic payments today exceed it, further payments need human approval"),
    "daily_total_cap_xrp": ("Daily total cap", "Automatic plus human-approved payments; cannot be exceeded even with approval"),
    "hard_cap_xrp": ("Per-payment hard cap", "Payments above this amount are denied outright; humans cannot approve them"),
    "escrow_threshold_xrp": ("Escrow threshold", "Above this amount, if the merchant supports it, funds go into escrow and are released only after verification"),
}

DEFAULTS = {"escrow_threshold_xrp": 5}   # 与 policy.load_policy 的默认值一致：文件里可以没有这一行

_lock = threading.Lock()


class PolicyValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("；".join(errors))
        self.errors = errors


def policy_path() -> Path:
    return Path(env("POLICY_PATH", str(ROOT / "policy.yaml")))


def current() -> dict[str, Any]:
    raw = yaml.safe_load(policy_path().read_text())
    return {"fields": [{"key": k, "label": label, "help": help_, "value_xrp": _fmt(_value(raw, k))}
                       for k, (label, help_) in FIELDS.items()],
            "history": history()}


def update(changes: dict[str, Any], source: str = "console") -> dict[str, Any]:
    """校验全部改动（含字段之间的关系），通过后原子写回 policy.yaml 并记审计；任何一项不合法都不写。"""
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise PolicyValidationError([f"{k} cannot be changed from the console" for k in unknown])
    with _lock:
        path = policy_path()
        text = path.read_text()
        raw = yaml.safe_load(text)
        old = {k: _value(raw, k) for k in FIELDS}
        new, errors = dict(old), []
        for key, value in changes.items():
            parsed = _parse(key, value, errors)
            if parsed is not None:
                new[key] = parsed
        errors += _relations(new)
        if errors:
            raise PolicyValidationError(errors)

        changed = {k: (old[k], new[k]) for k in FIELDS if new[k] != old[k]}
        for key, (_, value) in changed.items():
            text = _replace_value(text, key, _fmt(value))
        if changed:
            _atomic_write(path, text)
            now = time.time()
            with connect() as conn:
                conn.executemany(
                    "INSERT INTO policy_changes (ts, created_at, field, old_value, new_value, source) VALUES (?, ?, ?, ?, ?, ?)",
                    [(now, _iso(now), k, _fmt(a), _fmt(b), source) for k, (a, b) in changed.items()])
    return {**current(), "changed": sorted(changed)}


def history(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM policy_changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "label": FIELDS.get(r["field"], (r["field"],))[0]} for r in rows]


def _value(raw: dict, key: str) -> Decimal:
    return Decimal(str(raw.get(key, DEFAULTS.get(key)) if key in DEFAULTS else raw[key]))


def _parse(key: str, value: Any, errors: list[str]) -> Optional[Decimal]:
    label = FIELDS[key][0]
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        errors.append(f"{label}: “{value}” is not a number")
        return None
    if not d.is_finite():
        errors.append(f"{label}: must be a finite number")
    elif d < 0:
        errors.append(f"{label}: cannot be negative")
    elif d * policy.DROPS_PER_XRP != (d * policy.DROPS_PER_XRP).to_integral_value():
        errors.append(f"{label}: at most 6 decimal places (1 drop = 0.000001 XRP)")
    elif d > 100_000_000_000:
        errors.append(f"{label}: exceeds the total XRP supply")
    else:
        return d
    return None


def _relations(v: dict[str, Decimal]) -> list[str]:
    errors = []
    if v["hard_cap_xrp"] <= v["auto_limit_xrp"]:
        errors.append(f"Per-payment hard cap ({_fmt(v['hard_cap_xrp'])}) must be greater than the per-payment auto limit ({_fmt(v['auto_limit_xrp'])})")
    if v["daily_total_cap_xrp"] < v["daily_budget_xrp"]:
        errors.append(f"Daily total cap ({_fmt(v['daily_total_cap_xrp'])}) cannot be less than the daily auto budget ({_fmt(v['daily_budget_xrp'])})")
    if v["escrow_threshold_xrp"] >= v["hard_cap_xrp"]:
        errors.append(f"Escrow threshold ({_fmt(v['escrow_threshold_xrp'])}) must be less than the per-payment hard cap ({_fmt(v['hard_cap_xrp'])}), otherwise escrow is never used")
    return errors


def _replace_value(text: str, key: str, value: str) -> str:
    """只替换顶层 `key: 值` 这一行的值，行尾注释保留。"""
    pattern = re.compile(rf"^({re.escape(key)}:[ \t]*)([^#\n]*?)([ \t]*(#.*)?)$", re.MULTILINE)
    text, n = pattern.subn(lambda m: f"{m.group(1)}{value}{m.group(3) or ''}", text, count=1)
    if n != 1:                                  # 文件里没有这一行（用的是默认值）：追加
        text = text.rstrip("\n") + f"\n{key}: {value}\n"
    return text


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".policy-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        policy.load_policy(Path(tmp))          # 写回之前确认新文件能被正常加载
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f") if d else "0"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
