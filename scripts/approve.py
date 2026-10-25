"""命令行审批（没有界面时端到端测试用），通过 guard/api.py 的 HTTP 接口操作。

  .venv/bin/python scripts/approve.py list [--all]
  .venv/bin/python scripts/approve.py approve <id>
  .venv/bin/python scripts/approve.py reject <id> [--reason "..."]

可以在任何目录运行：API 地址从本项目的 .env 读取（GUARD_API_URL），每次都会先打印要请求的地址并做健康检查，
连不上、审批单已过期或已处理时都会明确报错，不会静默失败。

SIGNER_MODE=speculos 时，approve 会把后端准备好的未签名交易交给 Speculos 设备签名
（需要在设备屏幕上核对收款地址和金额并长按确认），再把签好的 blob 提交给后端。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
API = os.getenv("GUARD_API_URL") or dotenv_values(ROOT / ".env").get("GUARD_API_URL") or "http://127.0.0.1:8100"


def show_list(show_all: bool) -> None:
    rows = _call("GET", "/approvals", params={"status": "" if show_all else "pending"})
    if not rows:
        print("没有待审批的请求")
    for a in rows:
        q = a["quote"]
        expires = f"，{a['expires_in_seconds']} 秒后过期" if a["status"] == "pending" else ""
        print(f"#{a['id']} [{a['status']}{expires}] {a['rule_id']} {int(q['amount']) / 1_000_000} {q['asset']} → {q['payTo']}")
        print(f"    {a['url']}  用途：{a['purpose']}")
        print(f"    理由：{a['reason']}")
        if a.get("risk"):
            print(f"    风险（{a['risk'].get('source')}）：{a['risk']['risk_summary']}")
            for question in a["risk"].get("suggested_review_questions", []):
                print(f"      ? {question}")
        if a.get("payment_state"):
            print(f"    支付状态：{a['payment_state']}")


def approve(approval_id: int) -> None:
    current = _call("GET", f"/approvals/{approval_id}", quiet=True)
    body = {}
    if current.get("payee_unverified"):
        pay_to = current["quote"]["payTo"]
        print(f"⚠ 收款方未认证，可能是钓鱼地址：{pay_to}")
        body["confirm_payee_suffix"] = input("  确认要付款给这个地址，请输入收款地址的后 4 位：").strip()
    print(f"批准 #{approval_id} ...")
    a = _call("POST", f"/approvals/{approval_id}/approve", timeout=240, json=body)
    if a.get("payment_state") == "SIGNING" and a.get("signing_request"):
        a = _sign_on_device(approval_id, a["signing_request"])
    print(f"审批单状态：{a['status']}   支付状态：{a.get('payment_state')}")
    if a.get("tx_hash"):
        print(f"交易：{a['tx_hash']}\n浏览器：{a['explorer_url']}")
    if a.get("result") is not None:
        print(f"内容：{json.dumps(a['result'], ensure_ascii=False)[:200]}")
    if a.get("reject_reason") or a.get("payment_error"):
        print(f"原因：{a.get('reject_reason') or a.get('payment_error')}")


def _sign_on_device(approval_id: int, req: dict) -> dict:
    from guard.ledger_device import DeviceError, attach_signature, sign_unsigned
    print(f"请在设备屏幕上核对：收款地址 {req['destination']}，金额 {int(req['amount_drops']) / 1_000_000:g} XRP，"
          f"然后长按确认签名（{req['expires_in_seconds']} 秒内）")
    try:
        blob = attach_signature(req["unsigned_tx_hex"], sign_unsigned(req["unsigned_tx_hex"]))
    except DeviceError as exc:
        _call("POST", f"/approvals/{approval_id}/signature/cancel", json={"reason": f"设备签名失败：{exc}"})
        sys.exit(f"设备签名失败：{exc}")
    _call("POST", f"/approvals/{approval_id}/signature", json={"signed_blob": blob})
    print("已提交设备签名，等待上链 ...")
    for _ in range(120):
        a = _call("GET", f"/approvals/{approval_id}", quiet=True)
        if a["status"] != "approving":
            return a
        time.sleep(1)
    return a


def reject(approval_id: int, reason: str) -> None:
    a = _call("POST", f"/approvals/{approval_id}/reject", json={"reason": reason})
    print(f"#{approval_id} 状态：{a['status']}  原因：{a['reject_reason']}")


def _call(method: str, path: str, timeout: int = 30, quiet: bool = False, **kwargs):
    if not quiet:
        print(f"→ {method} {API}{path}")
    try:
        requests.get(f"{API}/audit", params={"limit": 1}, timeout=5).raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"守卫 API 不可用（{API}）：{exc}\n先在 {ROOT} 运行 .venv/bin/uvicorn guard.api:app --port 8100")
    try:
        resp = requests.request(method, API + path, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        sys.exit(f"请求守卫 API 失败：{exc}")
    if resp.status_code >= 400:
        sys.exit(f"HTTP {resp.status_code}：{resp.json().get('detail', resp.text)}")
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentGuard 命令行审批")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--all", action="store_true", help="包括已处理的")
    sub.add_parser("approve").add_argument("id", type=int)
    p_reject = sub.add_parser("reject")
    p_reject.add_argument("id", type=int)
    p_reject.add_argument("--reason", default="审批人拒绝")
    args = parser.parse_args()

    if args.cmd == "list":
        show_list(args.all)
    elif args.cmd == "approve":
        approve(args.id)
    else:
        reject(args.id, args.reason)


if __name__ == "__main__":
    main()
