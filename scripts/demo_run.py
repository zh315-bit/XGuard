"""现场演示：reset → 商户 E 设为 garbage → agent 依次处理 A、B、D、E，终端分段讲解。

运行（先 scripts/start_all.sh，并在浏览器打开控制台 http://127.0.0.1:5173）：
  .venv/bin/python scripts/demo_run.py [--e-mode normal|garbage]

需要签名的两步（B 的付款、E 的 escrow 锁款）会暂停并提示：请在控制台 Approvals 页用 Speculos 签名。
这个脚本不会自动确认设备：签名必须由人完成。
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV = dotenv_values(ROOT / ".env")
API = ENV.get("GUARD_API_URL") or "http://127.0.0.1:8100"
MERCHANT = ENV.get("MERCHANT_BASE_URL") or "http://127.0.0.1:8000"
CONSOLE = "http://127.0.0.1:5173"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
COLORS = {"a": "\033[32m", "b": "\033[33m", "d": "\033[31m", "e": "\033[36m"}

STEPS = {
    "a": ("第 1 步 · 商户 A：0.01 XRP", "已认证商户、小额 → 预期 AUTO：热钱包自动付款，链上带审计 Memo"),
    "b": ("第 2 步 · 商户 B：8 XRP", "超过单笔自动上限 1 XRP → 预期 HOLD：人工审批 + Ledger 签名，由金库付款"),
    "d": ("第 3 步 · 商户 D：报价调包", "判定后收款地址被换掉 → 预期 DENY（R8）：一分钱都不付"),
    "e": ("第 4 步 · 商户 E：先验货再放款", "6 XRP 走 escrow：金库锁款（Ledger 签名）→ 取货 → 验货 → 放款或到期退款"),
}


def banner(key: str) -> None:
    title, subtitle = STEPS[key]
    color = COLORS[key]
    line = "━" * 72
    print(f"\n{color}{line}\n{BOLD}  ▶ {title}{RESET}{color}\n    {subtitle}\n{line}{RESET}\n", flush=True)


def pause_for_signature(approval_id: str, key: str) -> None:
    what = "Payment（金库 → 商户 B）" if key == "b" else "Create Escrow（金库锁款给商户 E）"
    print(f"\n{BOLD}\033[43m\033[30m  ⏸  请在控制台用 Speculos 签名  {RESET}\n"
          f"    打开 {CONSOLE}/#approvals → 审批单 #{approval_id} → 「用 Ledger 签名」\n"
          f"    在右侧设备屏幕上核对：交易类型 {what}、收款地址和金额，然后在 ✓ 上按住约 3 秒\n"
          f"    {DIM}agent 正在 wait_for_approval 里等待，签完会自动继续{RESET}\n", flush=True)


def setup(e_mode: str) -> None:
    try:
        requests.post(f"{API}/api/demo/reset", timeout=10).raise_for_status()
        requests.post(f"{MERCHANT}/data/e/mode", json={"mode": e_mode}, timeout=5).raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"服务没起来或 DEMO_MODE 不是 true：{exc}\n先运行 scripts/start_all.sh")
    print(f"{BOLD}AgentGuard demo{RESET}  已清空本地状态，商户 E 设为 {e_mode} 模式。控制台：{CONSOLE}")


def run_agent() -> int:
    """运行 agent，逐行转发输出；看到购买某个商户时打印分段标题，看到待审批时暂停提示。"""
    proc = subprocess.Popen([sys.executable, "-u", "agent/agent.py"], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    current = None
    for line in proc.stdout:
        if line.startswith("HTTP Request"):
            continue
        match = re.search(r'"url": "[^"]*/data/([a-e])"', line)
        if match and "参数" in line and match.group(1) != current:
            current = match.group(1)
            banner(current)
        print(line, end="", flush=True)
        approval = re.search(r"approval_id\s+(\d+)", line)
        if approval and current in ("b", "e") and "pending" in _last_status(approval.group(1)):
            pause_for_signature(approval.group(1), current)
    return proc.wait()


def _last_status(approval_id: str) -> str:
    try:
        return requests.get(f"{API}/approvals/{approval_id}", timeout=5).json().get("status", "")
    except (requests.RequestException, ValueError):
        return ""


def follow_escrow() -> None:
    """E 验货不通过时，等到期自动退款（后台清理任务会提交 EscrowCancel），打印退款交易。"""
    escrows = requests.get(f"{API}/api/escrows", timeout=10).json()
    if not escrows:
        return
    e = escrows[0]
    verdict = (e.get("verification") or {}).get("verdict")
    if e["status"] == "released":
        print(f"\n{COLORS['e']}✓ 验货通过，已放款：{e['finish_tx_url']}{RESET}")
        return
    if verdict != "fail" and e["status"] not in ("refund_pending", "refunding"):
        return
    print(f"\n{COLORS['e']}{BOLD}验货不通过{RESET}{COLORS['e']}：{(e.get('verification') or {}).get('reason')}{RESET}")
    while e["status"] != "refunded":
        print(f"\r  钱锁在 escrow 里，{e['refund_in_seconds']:>3} 秒后到期自动退款…", end="", flush=True)
        time.sleep(2)
        e = requests.get(f"{API}/api/escrows", timeout=10).json()[0]
    print(f"\n{COLORS['e']}✓ 已退回金库：{e['cancel_tx_url']}{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--e-mode", default="garbage", choices=["normal", "garbage", "malformed"])
    args = parser.parse_args()
    setup(args.e_mode)
    code = run_agent()
    follow_escrow()
    print(f"\n{BOLD}演示结束。{RESET} 审计与链上交易：{CONSOLE}/#treasury")
    sys.exit(code)


if __name__ == "__main__":
    main()
