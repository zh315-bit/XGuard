"""结算对账：签名交易发出后结果不明（UNKNOWN_SETTLEMENT）时，按交易哈希查链决定最终状态。

规则（绝不在确认前重发）：
- 链上已验证且 tesSUCCESS → VALIDATED，预算转为已花费
- 链上已验证但失败（tec*）→ FAILED，释放预算（只烧了手续费，没有转账）
- 查不到：当前已验证账本 > LastLedgerSequence → 这笔交易再也不可能上链 → FAILED，释放预算
          否则保持 UNKNOWN_SETTLEMENT，下次再查
- 查询本身出错 → 保持 UNKNOWN_SETTLEMENT
"""

from dataclasses import dataclass
from typing import Optional

from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import Tx

from guard import audit, budget, payments
from guard import state_machine as sm
from guard.xrpl_client import get_client


@dataclass(frozen=True)
class ChainStatus:
    found: bool
    validated: bool = False
    success: bool = False
    result_code: Optional[str] = None


def lookup_tx(tx_hash: str) -> ChainStatus:
    """查链。网络错误直接抛出，由调用方当作“仍然未知”。"""
    resp = get_client().request(Tx(transaction=tx_hash))
    if not resp.is_successful():
        if resp.result.get("error") == "txnNotFound":
            return ChainStatus(found=False)
        raise RuntimeError(f"查询交易失败：{resp.result.get('error')}")
    code = resp.result.get("meta", {}).get("TransactionResult")
    validated = bool(resp.result.get("validated"))
    return ChainStatus(found=True, validated=validated, success=validated and code == "tesSUCCESS", result_code=code)


def latest_validated_ledger() -> int:
    return get_latest_validated_ledger_sequence(get_client())


def reconcile(payment_id: int) -> dict:
    """对一笔 UNKNOWN_SETTLEMENT 的请求查链，必要时推进状态。返回最新的行。"""
    row = payments.get(payment_id)
    if row["state"] != sm.UNKNOWN_SETTLEMENT:
        return row
    try:
        status = lookup_tx(row["tx_hash"])
        if status.found and status.validated:
            if status.success:
                _mark_validated(row)
            else:
                _mark_failed(row, f"交易已上链但失败：{status.result_code}")
        elif not status.found and latest_validated_ledger() > row["last_ledger_sequence"]:
            _mark_failed(row, f"已超过 LastLedgerSequence {row['last_ledger_sequence']} 仍未上链，交易已失效")
        # 其余情况（未验证 / 还没到期）：保持未知，等下次对账
    except sm.IllegalTransition:
        pass   # 另一个对账者（后台清理任务 / 执行器）已经先推进了状态，以最新结果为准
    except Exception as exc:  # noqa: BLE001 —— 查链失败不能推导出任何结论
        with_error = (row.get("error") or "") + f"；对账查询失败：{exc}"
        payments.set_error(payment_id, with_error)
    return payments.get(payment_id)


def reconcile_all_unknown() -> None:
    for row in payments.list_in_states((sm.UNKNOWN_SETTLEMENT,)):
        reconcile(row["id"])


def _mark_validated(row: dict) -> None:
    if row.get("method") == "escrow":
        # EscrowCreate 确认上链：钱锁住了，继续取货、验货
        from guard import escrow
        escrow.mark_locked(row["id"])
        escrow.deliver_and_verify(row["id"])
        return
    sm.advance(row["id"], sm.VALIDATED, error="链上已确认，但商户的资源响应丢失，资源未送达")
    budget.spend(row["id"])
    if row.get("decision_id"):
        # 按实际付款方记账：人工批准的是金库付款，自动付款才是热钱包
        reservation = budget.get(row["id"])
        if reservation and reservation["kind"] == budget.APPROVED_KIND:
            audit.mark_approved_paid(row["decision_id"], row["tx_hash"])
        else:
            audit.mark_paid(row["decision_id"], row["tx_hash"])


def _mark_failed(row: dict, reason: str) -> None:
    sm.advance(row["id"], sm.FAILED, error=reason)
    budget.release(row["id"])
    if row.get("method") == "escrow":
        from guard import escrow
        escrow.update(row["id"], status="failed")   # EscrowCreate 没上链：没有锁款
    if row.get("decision_id"):
        audit.mark_failed(row["decision_id"], reason, row["tx_hash"])
