"""链上金库保护：把金库交给 Ledger（Speculos）设备，最后禁用服务器上的金库主密钥。

步骤（可以重复运行，已完成的步骤会跳过）：
  1. 从设备取 44'/144'/0'/0/0 的 XRP 地址和公钥，写进 .env（TREASURY_SIGNER_ADDRESS / _PUBLIC_KEY）
  2. 金库执行 SetRegularKey，把 Regular Key 设为设备地址（用服务器上的金库主密钥签）
  3. 用设备代金库签一笔不动资金的 AccountSet（只带审计 Memo），确认上链成功 = Regular Key 生效
     （不再用付款测试：热钱包有 20 XRP 上限，往里转钱会超限）
  4. 打印确认、要求手动输入 yes，然后 AccountSet asfDisableMaster 禁用金库主密钥
  5. 打印金库的链上设置和浏览器链接；主密钥已禁用时，再用服务器 seed 试签一笔，证明它已经失效

运行：.venv/bin/python scripts/lock_treasury.py
前提：scripts/start_speculos.sh 已启动；签名时在前端 Approvals 页的设备画面或 VNC（vnc://127.0.0.1:41000，密码 speculos）上确认。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xrpl.core.binarycodec import encode
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import AccountInfo, AccountTx
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag, Memo, SetRegularKey, Transaction
from xrpl.transaction import autofill, submit, submit_and_wait
from xrpl.utils import str_to_hex

from guard.config import ROOT
from guard.ledger_device import attach_signature, device_identity, sign_unsigned
from guard.xrpl_client import address_of, explorer_tx_url, get_client, load_wallet

LSF_DISABLE_MASTER = 0x00100000
EXPLORER_ACCOUNT = "https://testnet.xrpl.org/accounts/{}"


def account_info(address: str) -> dict:
    return get_client().request(AccountInfo(account=address, ledger_index="validated")).result["account_data"]


def master_disabled(info: dict) -> bool:
    return bool(info.get("Flags", 0) & LSF_DISABLE_MASTER)


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def save_env(updates: dict[str, str]) -> None:
    """就地更新 .env 里的键（没有就追加），不动其他内容。"""
    path = ROOT / ".env"
    lines = path.read_text().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0]
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")


def submit_master(tx: Transaction, label: str) -> dict:
    """用服务器上的金库主密钥签名提交。"""
    result = submit_and_wait(tx, get_client(), load_wallet("treasury")).result
    code = result["meta"]["TransactionResult"]
    print(f"    {label}: {code}  {explorer_tx_url(result['hash'])}")
    if code != "tesSUCCESS":
        sys.exit(f"{label} 失败：{code}")
    return result


def device_signed_before(treasury: str, device_pubkey: str) -> str | None:
    """金库最近的交易里，有没有设备签名且成功上链的？有就返回交易哈希（第 3 步可以跳过）。"""
    txs = get_client().request(AccountTx(account=treasury, limit=50)).result.get("transactions", [])
    for t in txs:
        tx = t.get("tx_json") or t.get("tx") or {}
        if (t.get("validated") and tx.get("Account") == treasury and tx.get("SigningPubKey") == device_pubkey
                and t.get("meta", {}).get("TransactionResult") == "tesSUCCESS"):
            return t.get("hash") or tx.get("hash")
    return None


def device_test_signature(treasury: str, device_pubkey: str) -> None:
    """设备代金库签一笔 AccountSet（不设置任何标志、不动资金）：Account = 金库，SigningPubKey = 设备公钥，
    能上链就说明链上认可设备是金库的 Regular Key。"""
    client = get_client()
    tx = AccountSet(
        account=treasury,
        signing_pub_key=device_pubkey,
        # 留 10 分钟在设备上核对（autofill 默认只给约 1 分钟）
        last_ledger_sequence=get_latest_validated_ledger_sequence(client) + 150,
        memos=[Memo(memo_type=str_to_hex("agentguard/treasury"),
                    memo_data=str_to_hex(json.dumps({"action": "regular-key-test"})))],
    )
    unsigned = encode(autofill(tx, client).to_xrpl())
    print(f"    请在设备上核对：交易类型 AccountSet，账户是金库 {treasury}（不转任何钱），然后长按签名")
    blob = attach_signature(unsigned, sign_unsigned(unsigned))
    result = submit_and_wait(Transaction.from_blob(blob), client, autofill=False, check_fee=False).result
    code = result["meta"]["TransactionResult"]
    print(f"    设备代金库签名：{code}  {explorer_tx_url(result['hash'])}")
    if code != "tesSUCCESS":
        sys.exit(f"设备代金库签名失败：{code}，不会禁用主密钥")


def prove_master_dead(treasury: str) -> None:
    """用服务器 seed 试签一笔：应被拒绝（tefMASTER_DISABLED）。tef 类结果不会上账，没有任何副作用。"""
    tx = autofill(AccountSet(account=treasury), get_client())
    from xrpl.transaction import sign
    result = submit(sign(tx, load_wallet("treasury")), get_client()).result
    print(f"    用服务器上的金库 seed 试签：{result.get('engine_result')}（{result.get('engine_result_message')}）")


def print_settings(treasury: str) -> None:
    info = account_info(treasury)
    print(f"\n金库 {treasury}")
    print(f"    余额            {int(info['Balance']) / 1_000_000} XRP")
    print(f"    Regular Key     {info.get('RegularKey', '（未设置）')}")
    print(f"    主密钥          {'已禁用（lsfDisableMaster）' if master_disabled(info) else '仍然可用'}")
    print(f"    浏览器          {EXPLORER_ACCOUNT.format(treasury)}")
    if master_disabled(info):
        prove_master_dead(treasury)


def main() -> None:
    treasury = address_of("treasury")
    info = account_info(treasury)

    step(1, "从 Speculos 设备读取地址和公钥（44'/144'/0'/0/0）")
    device_address, device_pubkey = device_identity()
    print(f"    设备地址 {device_address}\n    设备公钥 {device_pubkey}")
    save_env({"TREASURY_SIGNER_ADDRESS": device_address, "TREASURY_SIGNER_PUBLIC_KEY": device_pubkey})

    if master_disabled(info):
        print("\n金库主密钥已经禁用，跳过 2-4。")
        if info.get("RegularKey") != device_address:
            sys.exit(f"警告：金库 Regular Key {info.get('RegularKey')} 不是当前设备 {device_address}")
        print_settings(treasury)
        return

    step(2, "SetRegularKey：金库的 Regular Key 设为设备地址")
    if info.get("RegularKey") == device_address:
        print("    已设置，跳过")
    else:
        submit_master(SetRegularKey(account=treasury, regular_key=device_address), "SetRegularKey")

    step(3, "用设备代金库签一笔不动资金的交易，确认 Regular Key 生效")
    proven = device_signed_before(treasury, device_pubkey)
    if proven:
        print(f"    链上已有设备代金库签名成功的交易，跳过：{explorer_tx_url(proven)}")
    else:
        device_test_signature(treasury, device_pubkey)

    step(4, "禁用金库主密钥（AccountSet asfDisableMaster）")
    print("    禁用后：")
    print("      - 服务器 .env 里的 TREASURY_SEED 再也不能代表金库签名")
    print(f"      - 金库只能由 Regular Key {device_address}（Speculos 设备）签名")
    print("      - SIGNER_MODE=software 的审批付款会失败，必须用 SIGNER_MODE=speculos")
    print("      - 要恢复，只能用设备签一笔 AccountSet clear asfDisableMaster")
    if input("    确认禁用请输入 yes：").strip() != "yes":
        print("    已取消，主密钥保持可用。")
        print_settings(treasury)
        return
    submit_master(AccountSet(account=treasury, set_flag=AccountSetAsfFlag.ASF_DISABLE_MASTER), "AccountSet asfDisableMaster")

    step(5, "金库链上设置")
    print_settings(treasury)


if __name__ == "__main__":
    main()
