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
        "AgentGuard 是你和钱包之间的支付守卫。所有付费 API（HTTP 402 / x402）都必须通过 fetch_paid_resource 访问，"
        "你没有私钥，也不能绕过它。每次购买都要给一个唯一的 request_id；重试同一次购买必须复用同一个 request_id，"
        "守卫保证同一个 request_id 只付一次钱。结果的 status：\n"
        "- paid：已付款，content 就是数据\n"
        "- pending_approval：需要人工审批，拿 approval_id 调用 wait_for_approval 等待，不要重复购买\n"
        "- denied / rejected / expired：没有付款，不要重试同一个 URL，向用户说明原因并换数据源或跳过\n"
        "- settlement_unknown：付款结果未知，绝不能用新的 request_id 重买"
    ),
)


@server.tool()
async def fetch_paid_resource(
    url: Annotated[str, Field(description="要访问的付费资源完整 URL")],
    purpose: Annotated[str, Field(description="为什么需要这份数据（写进审计日志，审批人会看到）")],
    max_price: Annotated[float, Field(description="你愿意为这份数据支付的最高价格，单位 XRP；报价超过它会直接拒绝")],
    request_id: Annotated[str, Field(min_length=1, max_length=128, description=(
        "这次购买的唯一 ID（例如 weekly-report-data-a）。重试同一次购买时必须用同一个 request_id，"
        "守卫会直接返回已有结果而不会再付钱；买另一份数据要用新的 request_id"))],
) -> dict[str, Any]:
    """通过 AgentGuard 访问一个 x402 付费资源。守卫先看报价，按策略决定：
    自动付款并返回数据（status=paid）；转人工审批（status=pending_approval，返回 approval_id——
    接下来应调用 wait_for_approval 等待，不要重新请求）；或拒绝付款（status=denied，说明原因——
    不要重试，换别的数据源或跳过这项）。"""
    try:
        limit = Decimal(str(max_price))
    except InvalidOperation:
        return {"status": "error", "reason": f"max_price 不是合法数字：{max_price}"}
    result = await asyncio.to_thread(fetch_paid, url, purpose, limit, agent_id=AGENT_ID, request_id=request_id)
    return views.present(result)


@server.tool()
async def list_data_sources() -> dict[str, Any]:
    """列出可购买的数据目录：每项有 URL、标题、描述和参考价格（XRP，商户自己声明的）。
    不确定该买什么数据时先调用它，再按任务需要挑选。参考价格只是商户的声明，实际以付款时的报价为准，
    守卫会在付款时按策略检查报价。"""
    return {"sources": data_sources()}


def data_sources() -> list[dict[str, Any]]:
    """读取 data_sources.yaml。只返回商户声明的信息，不含任何“好坏”标注。"""
    raw = yaml.safe_load((ROOT / "data_sources.yaml").read_text())
    base = env("MERCHANT_BASE_URL", "http://127.0.0.1:8000")
    return [{"url": s["url"].format(base=base), "title": s["title"], "description": s["description"],
             "reference_price_xrp": s["reference_price_xrp"]} for s in raw["sources"]]


@server.tool()
async def check_budget() -> dict[str, Any]:
    """查看今天的付款额度（单位 XRP）：自动付款日预算、已用/已预留、剩余，单笔自动上限、绝对上限，
    以及含人工批准在内的每日总上限。超过单笔自动上限或剩余预算的请求会进入人工审批。"""
    return await asyncio.to_thread(budget.summary, policy.load_policy())


@server.tool()
async def approval_status(
    approval_id: Annotated[int, Field(description="fetch_paid_resource 返回的 approval_id")],
) -> dict[str, Any]:
    """查询一次人工审批的当前状态（只查一次，不等待）。pending：还在等审批人；
    approved：已付款，content 是数据；rejected / expired：没有付款，reason 是原因——此时应换数据源或跳过。"""
    return await asyncio.to_thread(_approval_view, approval_id)


@server.tool()
async def wait_for_approval(
    approval_id: Annotated[int, Field(description="fetch_paid_resource 返回的 approval_id")],
    timeout_s: Annotated[int, Field(description=f"最多等待多少秒（上限 {MAX_WAIT_SECONDS}）")] = 150,
) -> dict[str, Any]:
    """等待人工审批出结果，直到 approved / rejected / expired 或超时。HOLD 之后调用这个。
    approved 时直接返回数据；rejected / expired 时返回原因，应换数据源或跳过；
    超时仍是 pending 时可以再调用一次继续等（审批单本身有有效期，过期后会变成 expired）。"""
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
