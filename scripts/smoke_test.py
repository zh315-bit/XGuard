"""冒烟测试：依次请求 /data/a、/data/b、/data/c、/data/d，打印守卫判定。

先启动商户：.venv/bin/uvicorn merchants.demo:app --port 8000
再运行：  .venv/bin/python scripts/smoke_test.py
期望：A → AUTO（带交易哈希和浏览器链接），B → HOLD（R4），C → HOLD（R3），
      D → DENY（R8 报价调包，不付款，审计里有原因）
前提：已运行 scripts/issue_creds.py 给 A、B 发凭证
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import uuid

import requests

from guard.config import require_env
from guard import audit
from guard.x402_gate import GateResult, fetch_paid
from guard.xrpl_client import address_of, xrp_balance

CASES = [
    ("/data/a", "周报需要 XRPL DEX 成交量", "AUTO", "DEFAULT"),
    ("/data/b", "周报需要巨鲸资金流向", "HOLD", "R4"),
    ("/data/c", "周报需要交易信号", "HOLD", "R3"),
    ("/data/d", "周报需要 DEX 成交量（镜像源）", "DENY", "R8"),
]


def show(result: GateResult, expected: str, expected_rule: str) -> bool:
    ok = (result.verdict, result.rule_id) == (expected, expected_rule) and result.paid == (expected == "AUTO")
    print(f"\n[{'PASS' if ok else 'FAIL'}] {result.url}")
    print(f"  判定   {result.verdict}  规则 {result.rule_id}  （期望 {expected} {expected_rule}）")
    print(f"  理由   {result.reason}")
    if result.quote:
        q = result.quote
        print(f"  报价   {q.amount_xrp} {q.asset} → {q.pay_to}  network={q.network}")
    if result.approval_id:
        print(f"  审批单 #{result.approval_id}（pending）")
    if result.tx_hash:
        print(f"  交易   {result.tx_hash}")
        print(f"  浏览器 {result.explorer_url}")
    if result.data is not None:
        print(f"  数据   {json.dumps(result.data, ensure_ascii=False)[:120]}")
    if result.error:
        print(f"  错误   {result.error}")
    if result.verdict == "DENY" and result.decision_id:
        row = audit.get_decision(result.decision_id)
        print(f"  审计   #{row['id']} status={row['status']} reason={row['reason']}")
        ok = ok and row["status"] == "denied" and bool(row["reason"])
    return ok


def main() -> None:
    base = require_env("MERCHANT_BASE_URL")
    try:
        requests.get(f"{base}/health", timeout=5).raise_for_status()
        requests.post(f"{base}/data/d/reset", timeout=5).raise_for_status()  # D 的调包计数清零，保证可重复
    except requests.RequestException:
        sys.exit(f"商户没启动：{base}/health 不可达")

    agent = address_of("agent")
    print(f"agent {agent} 余额 {xrp_balance(agent)} XRP")
    run = uuid.uuid4().hex[:8]   # 每次运行用新的 request_id，否则会命中幂等直接返回上次结果
    results = [show(fetch_paid(base + path, purpose, agent_id="smoke-test", request_id=f"{run}-{path}"), verdict, rule)
               for path, purpose, verdict, rule in CASES]
    print(f"\nagent 余额 {xrp_balance(agent)} XRP")
    print(f"\n{sum(results)}/{len(results)} 通过")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
