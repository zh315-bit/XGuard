"""AgentGuard 的 MCP server：agent 唯一能花钱的方式。stdio 传输，由 agent 进程拉起。

运行（一般不用手动跑）：.venv/bin/python -m guard.mcp_server

安全边界：
- 热钱包私钥只在这个进程里（x402_gate 内部的 agent_signer），金库私钥根本不在这个进程
- agent_id 由本进程的环境变量 AGENT_ID 决定，agent 不能自己指定
- 工具返回值只经 views 输出白名单字段，不返回任何钱包对象或 seed
"""

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import yaml
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from guard import approval_service, budget, maintenance, policy, views
from guard.approval_service import ApprovalError
from guard.config import ROOT, env
from guard.x402_gate import fetch_paid

MAX_WAIT_SECONDS = 600
POLL_SECONDS = 2
AGENT_ID = env("AGENT_ID", "demo-agent")

logging.getLogger("httpx").setLevel(logging.WARNING)  # xrpl-py 每次 RPC 都打一行 INFO，demo 时刷屏

server = MCPServer(
    name="agentguard",
    instructions=(
        "AgentGuard is the payment guard between you and the wallet. Every paid API (HTTP 402 / x402) must be accessed through fetch_paid_resource; "
        "you hold no private keys and cannot bypass it. Give every purchase a unique request_id; retries of the same purchase must reuse it, "
        "and the guard pays at most once per request_id. Result status:\n"
        "- paid: paid; content is the data\n"
        "- pending_approval: human approval needed; call wait_for_approval with approval_id; do not buy again\n"
        "- denied / rejected / expired / fetch_failed: nothing paid; do not retry the same URL; tell the user why and use another source or skip it\n"
        "- settlement_unknown: payment outcome unknown; never buy again with a new request_id"
    ),
)


@server.tool()
async def fetch_paid_resource(
    url: Annotated[str, Field(description="Full URL of the paid resource")],
    purpose: Annotated[str, Field(description="Why this data is needed (recorded in the audit log and shown to approvers)")],
    max_price: Annotated[float, Field(description="The most you are willing to pay for this data, in XRP; quotes above it are denied")],
    request_id: Annotated[str, Field(min_length=1, max_length=128, description=(
        "Unique ID for this purchase (e.g. weekly-report-data-a). Retries of the same purchase must use the same request_id; "
        "the guard returns the existing result without paying again. Use a new request_id for a different dataset"))],
) -> dict[str, Any]:
    """Access an x402 paid resource through AgentGuard. The guard inspects the quote and, by policy, either
    pays automatically and returns the data (status=paid); sends it for human approval (status=pending_approval with
    approval_id; call wait_for_approval next and do not request again); or refuses to pay (status=denied with a reason;
    do not retry, use another source or skip it)."""
    try:
        limit = Decimal(str(max_price))
    except InvalidOperation:
        return {"status": "error", "reason": f"max_price is not a valid number: {max_price}"}
    result = await asyncio.to_thread(fetch_paid, url, purpose, limit, agent_id=AGENT_ID, request_id=request_id)
    return views.present(result)


@server.tool()
async def list_data_sources() -> dict[str, Any]:
    """List the data catalog: each item has a URL, title, description and reference price (XRP, declared by the merchant).
    Call it first when unsure what to buy, then pick what the task needs. Reference prices are only the merchant's claims;
    the actual quote at payment time is what counts, and the guard checks it against policy."""
    return {"sources": data_sources()}


def data_sources() -> list[dict[str, Any]]:
    """读取 data_sources.yaml。只返回商户声明的信息，不含任何“好坏”标注。"""
    raw = yaml.safe_load((ROOT / "data_sources.yaml").read_text())
    base = env("MERCHANT_BASE_URL", "http://127.0.0.1:8000")
    return [{"url": s["url"].format(base=base), "title": s["title"], "description": s["description"],
             "reference_price_xrp": s["reference_price_xrp"]} for s in raw["sources"]]


@server.tool()
async def check_budget() -> dict[str, Any]:
    """Show today's payment limits (XRP): daily auto budget, spent/reserved, remaining, per-payment auto limit, hard cap,
    and the daily total cap including human-approved payments. Requests above the auto limit or remaining budget go to human approval."""
    return await asyncio.to_thread(budget.summary, policy.load_policy())


@server.tool()
async def approval_status(
    approval_id: Annotated[int, Field(description="approval_id returned by fetch_paid_resource")],
) -> dict[str, Any]:
    """Check the current state of a human approval once (no waiting). pending: still waiting for the approver;
    approved: paid, content is the data; rejected / expired: not paid, reason explains why; use another source or skip it."""
    return await asyncio.to_thread(_approval_view, approval_id)


@server.tool()
async def wait_for_approval(
    approval_id: Annotated[int, Field(description="approval_id returned by fetch_paid_resource")],
    timeout_s: Annotated[int, Field(description=f"Maximum seconds to wait (up to {MAX_WAIT_SECONDS})")] = 150,
) -> dict[str, Any]:
    """Wait for a human approval until approved / rejected / expired or the timeout. Call this after a HOLD.
    approved returns the data; rejected / expired returns the reason, so use another source or skip it;
    if still pending at the timeout you may call again (the approval itself expires and then becomes expired)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(max(timeout_s, 1), MAX_WAIT_SECONDS)
    while True:
        view = await asyncio.to_thread(_approval_view, approval_id)
        if view["status"] != "pending" or loop.time() >= deadline:
            return view
        await asyncio.sleep(POLL_SECONDS)


def _approval_view(approval_id: int) -> dict[str, Any]:
    maintenance.sweep()   # 让过期在等待期间就能被看到
    try:
        return views.approval_for_agent(approval_service.view(approval_id))
    except ApprovalError:
        return {"approval_id": approval_id, "status": "not_found"}


if __name__ == "__main__":
    server.run()
