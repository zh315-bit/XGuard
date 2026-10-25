"""AgentGuard：AI agent 和钱包之间的支付守卫（XRPL，x402）。

    from agentguard import Guard
    guard = Guard(policy_path="policy.yaml", agent_id="my-agent")
    result = guard.fetch_paid("https://api.example.com/report", purpose="周报数据", request_id="report-42")

result["status"]：paid（content 是数据）/ pending_approval（等人工审批）/ denied / …，见 README。
MCP server：命令行 `agentguard-mcp --agent-id my-agent`，或在代码里调用 serve_mcp()。
"""

from agentguard.client import Guard
from agentguard.mcp import serve as serve_mcp

__all__ = ["Guard", "serve_mcp"]
__version__ = "0.1.0"
