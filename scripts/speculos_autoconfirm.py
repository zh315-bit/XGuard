"""仅用于测试 / 无人值守彩排：在 Speculos（Stax）屏幕上自动翻页并“长按签名”。

演示时应由人在设备屏幕上确认，不要用这个脚本。start_all.sh / demo_run.py 都不会启动它；
必须显式设置 SPECULOS_AUTOCONFIRM=1 才能运行（防止误开后“人在环中”名存实亡）。

它不是盲签：逐页读取设备实际显示的文字（Speculos /events），
给了 --expect-destination / --expect-amount 时，只有设备上显示的收款地址和金额都与预期一致才会签名，
否则点 Reject。

  SPECULOS_AUTOCONFIRM=1 .venv/bin/python scripts/speculos_autoconfirm.py --expect-destination r... --expect-amount "XRP 8"
"""

import argparse
import os
import sys
import time

import requests

SPECULOS = "http://127.0.0.1:5001"
NEXT = (360, 625)          # Stax 底栏的 ">"
REJECT = (80, 625)         # Stax 底栏的 "Reject"
YES_REJECT = (200, 540)    # 点 Reject 后的二次确认 "Yes, reject"
HOLD = (335, 515)          # "Hold to sign" 的 ✓ 按钮


def screen_text(base: str) -> list[str]:
    events = requests.get(f"{base}/events", params={"currentscreenonly": "true"}, timeout=5).json()["events"]
    return [e["text"] for e in events]


def finger(base: str, action: str, xy: tuple[int, int]) -> None:
    requests.post(f"{base}/finger", json={"action": action, "x": xy[0], "y": xy[1]}, timeout=5)


def confirm(base: str, expect_destination: str | None, expect_amount: str | None, timeout: float) -> bool:
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        texts = screen_text(base)
        if "Review transaction" not in texts and not any(" of " in t for t in texts):
            time.sleep(0.5)                      # 还没进入审核界面
            continue
        seen.extend(t for t in texts if t not in seen)
        print("[device] " + " | ".join(texts))
        if "Hold to sign" in texts:
            joined = "".join(seen)               # 地址在屏幕上会被折成多行，拼起来再比
            ok = (not expect_destination or expect_destination in joined) and \
                 (not expect_amount or expect_amount in seen)
            if not ok:
                print(f"[device] 设备显示的内容与预期不符（期望 {expect_destination} / {expect_amount}），点 Reject")
                finger(base, "press-and-release", REJECT)
                time.sleep(1)
                finger(base, "press-and-release", YES_REJECT)
                return False
            finger(base, "press", HOLD)
            time.sleep(3)
            finger(base, "release", HOLD)
            print("[device] 已长按确认签名")
            return True
        finger(base, "press-and-release", NEXT)
        time.sleep(1)
    print("[device] 超时：没有等到签名界面", file=sys.stderr)
    return False


def main() -> None:
    if os.environ.get("SPECULOS_AUTOCONFIRM") != "1":
        sys.exit("speculos_autoconfirm.py 只用于测试：签名必须由人在设备上完成。确实要在测试里启用，请设置 SPECULOS_AUTOCONFIRM=1")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--speculos", default=SPECULOS)
    parser.add_argument("--expect-destination")
    parser.add_argument("--expect-amount", help='设备上显示的金额文字，例如 "XRP 8"')
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    ok = confirm(args.speculos, args.expect_destination, args.expect_amount, args.timeout)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
