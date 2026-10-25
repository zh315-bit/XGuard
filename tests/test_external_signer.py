"""ExternalSigner（Ledger / Speculos 签名路径）：后端准备未签名交易 → “设备”签名 → 提交 blob → 校验 → 付款。

用一个离线钱包扮演设备；提交的 blob 只要有一项与审批单不一致，就必须拒绝且不付款。
"""

import asyncio
import json

import pytest
from xrpl.core.binarycodec import decode, encode, encode_for_signing
from xrpl.core.keypairs import sign as keypair_sign
from xrpl.wallet import Wallet

from guard import approval_service, payments, signing_requests
from guard import state_machine as sm
from guard.approval_service import ApprovalError
from guard.signers import ExternalSigner
from fakes import OTHER, PAYEE, decode_memos, fetch, merchant, offline, terms  # noqa: F401

TREASURY = "rwzHwgxw5UYVgaq2dWctCKGKcCppKnPkUB"


@pytest.fixture
def device():
    return Wallet.create()


def device_sign(tx_json: dict, device: Wallet) -> str:
    """模拟设备：对 encode_for_signing 签名，结果放进 TxnSignature（与前端 xrpl.js 的做法相同）。"""
    signature = keypair_sign(bytes.fromhex(encode_for_signing(tx_json)), device.private_key)
    return encode({**tx_json, "TxnSignature": signature})


def run_external(merchant, device, act, timeout=10.0):
    """HOLD → 用 ExternalSigner 批准 → act(审批视图) 扮演前端 → 等后台付款结束，返回最终审批视图。"""
    merchant.default_terms = terms(8_000_000)
    r = fetch(merchant)

    async def scenario():
        signer = ExternalSigner(TREASURY, device.public_key, r.approval_id, r.payment_id, timeout)
        view = await approval_service.approve(r.approval_id, signer)
        await asyncio.to_thread(act, view)
        await asyncio.gather(*approval_service._background, return_exceptions=True)
    asyncio.run(scenario())
    return r, approval_service.view(r.approval_id)


def test_device_signature_pays(merchant, offline, device):
    def act(view):
        req = view["signing_request"]
        assert view["payment_state"] == sm.SIGNING and req["status"] == "pending"
        assert (req["account"], req["signing_pub_key"]) == (TREASURY, device.public_key)
        assert (req["destination"], req["amount_drops"]) == (PAYEE, "8000000")
        assert req["last_ledger_sequence"] == 1_000                     # 必须带 LastLedgerSequence
        approval_service.submit_signature(view["id"], device_sign(req["tx_json"], device))

    r, final = run_external(merchant, device, act)

    assert (final["status"], final["payment_state"]) == ("approved", sm.RESOURCE_DELIVERED)
    assert len(merchant.payment_headers) == 1 and offline.calls == 0
    memo = json.loads(decode_memos(merchant.payment_headers[0])[1]["MemoData"])
    assert memo == {"verdict": "HOLD", "rule_id": "R4", "approval_id": r.approval_id}


@pytest.mark.parametrize("tamper, message", [
    (lambda tx: {**tx, "Destination": OTHER}, "differs"),                  # 改收款地址
    (lambda tx: {**tx, "Amount": "9000000"}, "differs"),                   # 改金额
    (lambda tx: {**tx, "Fee": "5000"}, "differs"),                         # 改其他字段
], ids=["destination", "amount", "fee"])
def test_tampered_blob_rejected(merchant, offline, device, tamper, message):
    errors = []

    def act(view):
        tx = tamper(view["signing_request"]["tx_json"])
        with pytest.raises(ApprovalError) as exc:
            approval_service.submit_signature(view["id"], device_sign(tx, device))
        errors.append(exc.value)

    r, final = run_external(merchant, device, act)

    assert errors[0].status_code == 400 and message in str(errors[0])
    assert final["status"] == "rejected" and final["payment_state"] == sm.FAILED
    assert merchant.payment_headers == []


def test_forged_signature_rejected(merchant, offline, device):
    errors = []

    def act(view):
        tx = view["signing_request"]["tx_json"]
        forged = device_sign(tx, Wallet.create())                       # 别的钥匙签的
        forged = encode({**decode(forged), "SigningPubKey": tx["SigningPubKey"]})
        with pytest.raises(ApprovalError) as exc:
            approval_service.submit_signature(view["id"], forged)
        errors.append(str(exc.value))

    _, final = run_external(merchant, device, act)

    assert "Signature verification failed" in errors[0]
    assert final["payment_state"] == sm.FAILED and merchant.payment_headers == []


def test_rejected_on_device(merchant, offline, device):
    _, final = run_external(merchant, device, lambda view: approval_service.cancel_signature(view["id"], "设备上拒绝"))
    assert final["status"] == "rejected" and "设备上拒绝" in final["reject_reason"]
    assert final["payment_state"] == sm.FAILED and merchant.payment_headers == []


def test_signing_timeout_fails_safely(merchant, offline, device):
    _, final = run_external(merchant, device, lambda view: None, timeout=1.0)
    assert final["payment_state"] == sm.FAILED and "Timed out" in final["reject_reason"]
    assert final["signing_request"]["status"] == signing_requests.EXPIRED
    assert merchant.payment_headers == []


def test_second_submission_rejected(merchant, offline, device):
    errors = []

    def act(view):
        blob = device_sign(view["signing_request"]["tx_json"], device)
        approval_service.submit_signature(view["id"], blob)
        with pytest.raises(ApprovalError) as exc:
            approval_service.submit_signature(view["id"], blob)
        errors.append(exc.value.status_code)

    _, final = run_external(merchant, device, act)
    assert errors == [400] and len(merchant.payment_headers) == 1


def test_signer_mode_switch(monkeypatch):
    from guard import signers
    monkeypatch.setenv("SIGNER_MODE", "speculos")
    monkeypatch.setenv("TREASURY_SIGNER_PUBLIC_KEY", "03" + "AB" * 32)
    assert isinstance(signers.approval_signer(1, 1, 60), ExternalSigner)
    monkeypatch.setenv("SIGNER_MODE", "software")
    assert isinstance(signers.approval_signer(1, 1, 60), signers.SoftwareSigner)
