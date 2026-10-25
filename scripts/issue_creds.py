"""issuer 给 merchant_a、merchant_b 签发 AgentGuardVerifiedMerchant 凭证，商户各自 CredentialAccept。

运行：.venv/bin/python scripts/issue_creds.py
可重复运行：已签发 / 已接受的步骤会跳过。merchant_c 故意不发。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xrpl.models.transactions import CredentialAccept, CredentialCreate, Transaction
from xrpl.transaction import submit_and_wait
from xrpl.utils import str_to_hex
from xrpl.wallet import Wallet

from guard import credentials
from guard.policy import load_policy
from guard.xrpl_client import explorer_tx_url, get_client, load_wallet

MERCHANTS = ["merchant_a", "merchant_b"]


def submit(tx: Transaction, wallet: Wallet, label: str) -> None:
    result = submit_and_wait(tx, get_client(), wallet).result
    code = result["meta"]["TransactionResult"]
    print(f"  {label}: {code}  {explorer_tx_url(result['hash'])}")
    if code != "tesSUCCESS":
        sys.exit(f"{label} 失败：{code}")


def issue(issuer: Wallet, merchant: Wallet, cred_type: str) -> None:
    cred_hex = str_to_hex(cred_type)
    node = credentials.fetch_credential(merchant.address, issuer.address, cred_type)
    if node is None:
        submit(CredentialCreate(account=issuer.address, subject=merchant.address, credential_type=cred_hex),
               issuer, "CredentialCreate")
        node = credentials.fetch_credential(merchant.address, issuer.address, cred_type)
    else:
        print("  CredentialCreate: 已存在，跳过")

    if node["Flags"] & credentials.LSF_ACCEPTED:
        print("  CredentialAccept: 已接受，跳过")
    else:
        submit(CredentialAccept(account=merchant.address, issuer=issuer.address, credential_type=cred_hex),
               merchant, "CredentialAccept")


def main() -> None:
    cfg = load_policy()
    issuer = load_wallet("issuer")
    if issuer.address != cfg.credential_issuer:
        sys.exit(f"policy.yaml 的 issuer {cfg.credential_issuer} 与 .env 的 ISSUER 不一致")

    for role in MERCHANTS:
        merchant = load_wallet(role)
        print(f"{role} {merchant.address}")
        issue(issuer, merchant, cfg.credential_type)

    # 用守卫自己的查询逻辑复核一遍（包括 merchant_c 应为 False）
    credentials.clear_cache()
    for role in MERCHANTS + ["merchant_c"]:
        address = load_wallet(role).address
        ok = credentials.is_verified(address, cfg.credential_issuer, cfg.credential_type)
        print(f"复核 {role}: {'有效' if ok else '无凭证'}")


if __name__ == "__main__":
    main()
