"""演示：模拟服务器被攻破——绕过守卫，直接用热钱包私钥向一个陌生地址转 0.01 XRP。

这笔交易不经过守卫的任何代码（没有报价、判定、审计记录），只有链上能看到。
独立哨兵（watchdog/sentinel.py）应把它标记为 UNAUTHORIZED，控制台所有页面顶部出现红色警报。

- 只在 DEMO_MODE=true 时可用，并且要手动输入 yes 确认（会真实花掉热钱包 0.01 XRP + 手续费，testnet）
- 陌生地址：--to 指定；不指定就从 testnet faucet 新建一个（XRPL 上的新账户要先满足储备才能收 0.01 XRP）

运行：.venv/bin/python scripts/simulate_compromise.py [--to rXXXX]
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xrpl.clients import JsonRpcClient  # noqa: E402
from xrpl.models.transactions import Payment  # noqa: E402
from xrpl.transaction import submit_and_wait  # noqa: E402
from xrpl.utils import ripple_time_to_posix  # noqa: E402
from xrpl.wallet import generate_faucet_wallet  # noqa: E402

from guard.config import env  # noqa: E402
from guard.xrpl_client import address_of, explorer_tx_url, get_client, load_wallet  # noqa: E402

AMOUNT_DROPS = "10000"   # 0.01 XRP
FAUCET_RPC = "https://s.altnet.rippletest.net:51234/"
FAUCET_HOST = "faucet.altnet.rippletest.net"


def stranger_address() -> str:
    print("从 testnet faucet 新建一个陌生账户（约 10 秒）…")
    return generate_faucet_wallet(JsonRpcClient(FAUCET_RPC), faucet_host=FAUCET_HOST).address


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", help="收款的陌生地址（默认从 faucet 新建）")
    args = parser.parse_args()

    if env("DEMO_MODE", "false").lower() != "true":
        sys.exit("只能在演示环境运行：.env 里设置 DEMO_MODE=true")

    hot = load_wallet("agent")
    if hot.address != address_of("agent"):
        sys.exit("AGENT_SEED 和 AGENT_ADDRESS 不一致")
    watched = {address_of("agent"), address_of("treasury")}
    print("⚠️  模拟服务器被攻破：绕过守卫，直接用热钱包私钥转账")
    print(f"   付款方：热钱包 {hot.address}")
    print("   金额：  0.01 XRP（testnet，真实上链，守卫数据库里不会有任何记录）")
    if input("确认执行？输入 yes 继续：").strip() != "yes":
        sys.exit("已取消")

    destination = args.to or stranger_address()
    if destination in watched:
        sys.exit("收款地址必须是陌生地址，不能是金库或热钱包")
    print(f"   收款方：{destination}")

    tx = Payment(account=hot.address, destination=destination, amount=AMOUNT_DROPS)
    started = time.time()
    result = submit_and_wait(tx, get_client(), hot).result
    validated_at = time.time()
    code = result.get("meta", {}).get("TransactionResult")
    close = result.get("close_time_iso") or (
        datetime.fromtimestamp(ripple_time_to_posix(result["tx_json"]["date"]), timezone.utc).isoformat()
        if result.get("tx_json", {}).get("date") is not None else "?")

    print()
    print(f"结果：     {code}（账本 {result.get('ledger_index')}，账本关闭时间 {close}）")
    print(f"交易哈希： {result['hash']}")
    print(f"浏览器：   {explorer_tx_url(result['hash'])}")
    print(f"本机确认验证：{datetime.fromtimestamp(validated_at, timezone.utc).isoformat(timespec='milliseconds')}"
          f"（提交到验证 {validated_at - started:.1f} 秒）")
    print()
    print("请观察控制台的警报：http://127.0.0.1:5173 —— 任何页面顶部应出现红色 UNAUTHORIZED 横幅")


if __name__ == "__main__":
    main()
