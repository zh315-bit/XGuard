"""守卫的 HTTP 接口（控制台 / approve.py / agent 任务上报 / 不走 MCP 的调用方），端口 8100。

运行：.venv/bin/uvicorn guard.api:app --port 8100
金库签名：SIGNER_MODE=speculos 时在设备上完成（本进程没有能动金库的私钥）；
SIGNER_MODE=software 时用 .env 里的金库 seed（备用）。返回值不含任何密钥。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Literal, Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from guard import (approval_service, approvals, audit, budget, credentials, escrow, hot_wallet, llm, maintenance,
                   mission_runner, missions, monitor, payments, policy, policy_settings, report, treasury, views)
from guard.approval_service import ApprovalError
from guard.policy_settings import PolicyValidationError
from guard.config import env
from guard.db import connect
from guard.settlement import reconcile
from guard.signers import signer_mode
from guard.x402_gate import fetch_paid, from_row
from guard.xrpl_client import address_of, explorer_tx_url

SWEEP_SECONDS = 5
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """后台清理任务：审批过期、预留过期、未知结算对账、escrow 到期退款 / 放款重试。
    不依赖控制台是否打开：没人看页面，到期的 escrow 也会自动 EscrowCancel。"""
    async def loop():
        while True:
            try:
                await asyncio.to_thread(maintenance.sweep)
            except Exception:  # noqa: BLE001 —— 下一轮再试
                log.exception("后台清理失败")
            await asyncio.sleep(SWEEP_SECONDS)
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="AgentGuard", lifespan=lifespan)


class PaymentBody(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=128, description="Idempotency key: a request_id is paid at most once")
    url: str
    purpose: str
    max_price_xrp: Optional[Decimal] = None


class RejectBody(BaseModel):
    reason: str = "Rejected by the approver"


class ApproveBody(BaseModel):
    confirm_payee_suffix: Optional[str] = Field(default=None, max_length=8,
                                                description="Required for R3 (unverified payee) approvals: last 4 characters of the payee address")


class SignatureBody(BaseModel):
    signed_blob: str = Field(min_length=10, description="Transaction blob signed by the device (hex)")


class TopupBody(BaseModel):
    amount_xrp: Decimal = Field(gt=0)
    reason: str = "Hot wallet top-up"


class MissionBody(BaseModel):
    """任务文本只是 agent 的输入，守卫的判定完全不看它。"""
    task: str = Field(min_length=1, max_length=mission_runner.MAX_TASK_CHARS)


class MissionRegisterBody(BaseModel):
    task: str = Field(min_length=1, max_length=mission_runner.MAX_TASK_CHARS)
    pid: Optional[int] = None


class ModeBody(BaseModel):
    mode: str


class MissionEventBody(BaseModel):
    kind: str
    name: Optional[str] = None
    payload: Any = None


class MissionFinishBody(BaseModel):
    status: Literal["done", "failed", "llm_unavailable"] = "done"
    report: Optional[str] = Field(default=None, max_length=20000)


class AckBody(BaseModel):
    note: str = Field(default="Handled by the operator", max_length=300)


class LLMEventBody(BaseModel):
    source: Literal["agent"] = "agent"
    ok: bool
    kind: Optional[str] = Field(default=None, max_length=32)
    detail: Optional[str] = Field(default=None, max_length=300)


@app.post("/payments")
async def create_payment(body: PaymentBody) -> dict[str, Any]:
    result = await asyncio.to_thread(fetch_paid, body.url, body.purpose, body.max_price_xrp,
                                     agent_id=body.agent_id, request_id=body.request_id)
    return views.present(result)


@app.get("/payments/{agent_id}/{request_id}")
def get_payment(agent_id: str, request_id: str) -> dict[str, Any]:
    row = payments.get_by_request(agent_id, request_id)
    if row is None:
        raise HTTPException(404, "No such request_id")
    return views.present(from_row(reconcile(row["id"])))


@app.get("/payments")
def list_payments(limit: int = 100) -> list[dict[str, Any]]:
    """Payment Queue：所有支付请求（最新在前），含判定、报价、命中规则、风险解释、状态。"""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM payment_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        r = from_row(dict(row))
        out.append({**views.present(r), "id": r.payment_id, "agent_id": row["agent_id"],
                    "created_at": row["created_at"], "updated_at": row["updated_at"], "purpose": row["purpose"],
                    "method": row["method"], "risk": r.risk})
    return out


@app.get("/approvals")
def list_approvals(status: Optional[str] = "pending") -> list[dict[str, Any]]:
    """status 传空字符串返回全部。每条都带风险解释（risk）和对应支付请求的状态。"""
    maintenance.sweep()
    return [approval_service.view(a["id"]) for a in approvals.list_by_status(status or None)]


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: int) -> dict[str, Any]:
    try:
        return approval_service.view(approval_id)
    except ApprovalError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: int, body: Optional[ApproveBody] = None) -> dict[str, Any]:
    """software：直接签名付款，返回最终结果。speculos：返回 payment_state=SIGNING 和 signing_request，
    前端 / CLI 用设备签好后调用 /signature。R3 审批单必须在 body 里带 confirm_payee_suffix。"""
    try:
        return await approval_service.approve(approval_id,
                                              confirm_payee_suffix=body.confirm_payee_suffix if body else None)
    except ApprovalError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/approvals/{approval_id}/signature")
def submit_signature(approval_id: int, body: SignatureBody) -> dict[str, Any]:
    try:
        return approval_service.submit_signature(approval_id, body.signed_blob)
    except ApprovalError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/approvals/{approval_id}/signature/cancel")
def cancel_signature(approval_id: int, body: Optional[RejectBody] = None) -> dict[str, Any]:
    return approval_service.cancel_signature(approval_id, (body or RejectBody(reason="Signing rejected on the device")).reason)


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: int, body: Optional[RejectBody] = None) -> dict[str, Any]:
    try:
        return approval_service.reject(approval_id, (body or RejectBody()).reason)
    except ApprovalError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.get("/audit")
def get_audit(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        by_decision = {r["decision_id"]: dict(r) for r in conn.execute(
            "SELECT p.id, p.decision_id, p.method, b.kind FROM payment_requests p "
            "LEFT JOIN budget_reservations b ON b.payment_request_id = p.id WHERE p.decision_id IS NOT NULL")}
    return [{**d, "explorer_url": explorer_tx_url(d["tx_hash"]) if d["tx_hash"] else None,
             "payer": _payer(by_decision.get(d["id"]))} for d in audit.list_decisions(limit)]


def _payer(payment: Optional[dict]) -> Optional[str]:
    """付款方：由付款方式和预算预留类型决定（与链上 Account 一致），而不是从审计状态猜。"""
    if payment is None:
        return None
    if payment["method"] == "topup":
        return "Treasury → hot wallet (top-up, Ledger-signed)"
    if payment["method"] == "escrow":
        return "Treasury (escrow lock, Ledger-signed)"
    return {"approved": "Treasury (Ledger-signed)", "auto": "Hot wallet"}.get(payment["kind"])


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """控制台需要的非敏感配置。"""
    return {"signer_mode": signer_mode(), "speculos_url": env("SPECULOS_PUBLIC_URL", "http://127.0.0.1:5001"),
            "speculos_model": env("SPECULOS_MODEL", "STAX"), "demo_mode": env("DEMO_MODE", "false") == "true",
            "treasury_address": address_of("treasury"),
            "device_address": env("TREASURY_SIGNER_ADDRESS", "") or None}


@app.get("/api/treasury")
def get_treasury() -> dict[str, Any]:
    """Treasury & Audit：预算（已花 / 预留 / 剩余）、热钱包和金库余额、金库是否已交给设备、主密钥状态。"""
    cfg = policy.load_policy()
    return {**treasury.status(), "budget": budget.summary(cfg), "budget_detail": budget.detail_today()}


@app.get("/health")
def health() -> dict[str, Any]:
    """健康检查：守卫 API 本身 + 独立哨兵的连接状态（哨兵断线不影响 API 的 200，但会如实显示）。"""
    sentinel = monitor.health()
    return {"api": "ok", "sentinel": sentinel["status"], "sentinel_endpoint": sentinel["endpoint"],
            "sentinel_heartbeat_age_seconds": sentinel["heartbeat_age_seconds"]}


@app.get("/api/monitor")
def get_monitor() -> dict[str, Any]:
    """独立哨兵（watchdog/sentinel.py）的连接状态和对账结果。哨兵是另一个进程，这里只读它写的表。"""
    return monitor.view()


@app.post("/api/monitor/alerts/{tx_hash}/ack")
def ack_alert(tx_hash: str, body: Optional[AckBody] = None) -> dict[str, Any]:
    row = monitor.acknowledge(tx_hash, (body or AckBody()).note)
    if row is None or row["status"] != monitor.UNAUTHORIZED:
        raise HTTPException(404, "No UNAUTHORIZED alert with this tx hash")
    return row


class SettingsBody(BaseModel):
    changes: dict[str, str] = Field(description="Items to change; keys as in GET /api/settings, values are XRP amounts (strings)")


@app.get("/api/llm/status")
def get_llm_status() -> dict[str, Any]:
    return llm.status()


@app.post("/api/llm/events")
def post_llm_event(body: LLMEventBody) -> dict[str, Any]:
    """agent 进程上报自己最近一次 LLM 调用的结果（它不 import guard）。"""
    llm.record(body.source, body.ok, body.kind, body.detail)
    return {"ok": True}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return policy_settings.current()


@app.post("/api/settings")
def update_settings(body: SettingsBody) -> dict[str, Any]:
    """校验后写回 policy.yaml，立即生效（每次判定都会重新读取），并记入策略修改审计。"""
    try:
        return policy_settings.update(body.changes, source="console")
    except PolicyValidationError as exc:
        raise HTTPException(400, {"message": str(exc), "errors": exc.errors}) from exc


@app.get("/api/report/today")
def get_report_today() -> dict[str, Any]:
    return report.today()


@app.get("/api/escrows")
def list_escrows() -> list[dict[str, Any]]:
    """escrow 全过程：锁款交易、交付内容、验货结论、放款 / 退款交易、退款倒计时。不含原像。"""
    maintenance.sweep()   # 顺带推进到期退款
    return [escrow.public_view(e) for e in escrow.list_all()]


@app.post("/api/escrows/{payment_id}/refund")
def refund_escrow(payment_id: int) -> dict[str, Any]:
    if escrow.get(payment_id) is None:
        raise HTTPException(404, "No such escrow")
    try:
        escrow.refund(payment_id)
    except escrow.TooEarlyToCancel as exc:
        raise HTTPException(409, str(exc)) from exc
    return escrow.public_view(escrow.get(payment_id))


@app.get("/api/hot-wallet")
def get_hot_wallet() -> dict[str, Any]:
    return hot_wallet.status()


@app.post("/api/hot-wallet/topup")
def request_topup(body: TopupBody) -> dict[str, Any]:
    """补款只能由金库付款：这里只生成审批单，必须在 Approvals 页人工批准 + Ledger 签名。"""
    try:
        return hot_wallet.request_topup(policy.xrp_to_drops(body.amount_xrp), body.reason)
    except hot_wallet.CapExceeded as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/missions")
def start_mission(body: MissionBody) -> dict[str, Any]:
    """控制台发起任务：后台启动一次 agent 运行。同一时间只允许一个任务，运行中再提交返回 409。"""
    try:
        return {"id": mission_runner.start(body.task)}
    except mission_runner.MissionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/missions/register")
def register_mission(body: MissionRegisterBody) -> dict[str, Any]:
    """命令行 / demo_run 启动的 agent 自己登记任务记录（只是为了在 Mission 页面显示）。"""
    busy = mission_runner.running_mission()
    if busy:
        raise HTTPException(409, f"Mission #{busy['id']} is running")
    return {"id": missions.create(body.task, source="cli", pid=body.pid)}


@app.post("/api/missions/{mission_id}/cancel")
def cancel_mission(mission_id: int) -> dict[str, Any]:
    try:
        return mission_runner.cancel(mission_id)
    except mission_runner.NotRunning as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/missions/presets")
def mission_presets() -> list[dict[str, str]]:
    from agent import presets
    return presets.presets(env("MERCHANT_BASE_URL", "http://127.0.0.1:8000"))


@app.post("/api/missions/{mission_id}/events")
def add_mission_event(mission_id: int, body: MissionEventBody) -> dict[str, Any]:
    missions.add_event(mission_id, body.kind, body.name, body.payload)
    return {"ok": True}


@app.post("/api/missions/{mission_id}/finish")
def finish_mission(mission_id: int, body: MissionFinishBody) -> dict[str, Any]:
    missions.finish(mission_id, body.status, body.report)
    return {"ok": True}


@app.get("/api/missions/latest")
def latest_mission() -> Optional[dict[str, Any]]:
    return missions.latest()


@app.get("/api/demo/merchant-e-mode")
def get_merchant_e_mode() -> dict[str, Any]:
    merchant = env("MERCHANT_BASE_URL", "http://127.0.0.1:8000")
    try:
        return requests.get(f"{merchant}/data/e/mode", timeout=5).json()
    except requests.RequestException as exc:
        raise HTTPException(502, f"Cannot reach the demo merchant: {exc}") from exc


@app.post("/api/demo/merchant-e-mode")
def set_merchant_e_mode(body: ModeBody) -> dict[str, Any]:
    """演示用：切换商户 E 交付正常数据还是垃圾数据。只在 DEMO_MODE=true 时可用。"""
    if env("DEMO_MODE", "false").lower() != "true":
        raise HTTPException(403, "Only available when DEMO_MODE=true")
    merchant = env("MERCHANT_BASE_URL", "http://127.0.0.1:8000")
    resp = requests.post(f"{merchant}/data/e/mode", json={"mode": body.mode}, timeout=5)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.json().get("detail", resp.text))
    return resp.json()


@app.post("/api/demo/reset")
def demo_reset() -> dict[str, Any]:
    """清空本地审计、预算、审批、支付请求（R6 频率也由它们统计），并重置商户 D 的调包计数。
    链上交易不会也不需要撤销。只在 DEMO_MODE=true 时可用。"""
    if env("DEMO_MODE", "false").lower() != "true":
        raise HTTPException(403, "Demo reset is only available when DEMO_MODE=true in .env")
    if mission_runner.running_mission():
        raise HTTPException(409, "A mission is running: cancel it or wait for it to finish before resetting")
    tables = ("escrows", "signing_requests", "budget_reservations", "approvals", "payment_requests", "decisions",
              "mission_events", "missions")
    with connect() as conn:
        cleared = {t: conn.execute(f"DELETE FROM {t}").rowcount for t in tables}
    credentials.clear_cache()
    merchant = env("MERCHANT_BASE_URL", "http://127.0.0.1:8000")
    try:
        requests.post(f"{merchant}/data/d/reset", timeout=5).raise_for_status()
        merchant_reset = True
    except requests.RequestException:
        merchant_reset = False
    return {"cleared": cleared, "merchant_d_reset": merchant_reset}
