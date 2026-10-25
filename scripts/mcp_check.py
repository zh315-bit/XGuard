"""不经过 LLM，直接通过 Strands MCPClient 调用 AgentGuard 的 4 个 MCP 工具，并检查返回值里没有任何 seed。

前提：商户（8000）在运行；B 的审批要用 approve.py 处理，所以需要守卫 API（8100）。
运行：.venv/bin/python scripts/mcp_check.py [--wait]
  --wait：B 进入 HOLD 后调用 wait_for_approval 等你在另一个终端批准
"""

import json
import sys
import uuid
from pathlib import Path

import requests
from dotenv import dotenv_values
from mcp import StdioServerParameters, stdio_client
from strands.tools.mcp import MCPClient

ROOT = Path(__file__).resolve().parent.parent
ENV = dotenv_values(ROOT / ".env")   # 只在本进程读，用来做泄漏检查，不传给 MCP server
SEEDS = [v for k, v in ENV.items() if k.endswith("_SEED") and v]
BASE = ENV["MERCHANT_BASE_URL"]


def call(client: MCPClient, name: str, **args) -> dict:
    result = client.call_tool_sync(f"check-{name}", name, args)
    text = json.dumps(result, ensure_ascii=False, default=str)
    leaked = [s for s in SEEDS if s in text]
    assert not leaked, f"{name} 的返回值里出现了 seed！"
    payload = result.get("structuredContent") or result
    print(f"\n>>> {name}({json.dumps(args, ensure_ascii=False)})\n{json.dumps(payload, ensure_ascii=False, indent=2)[:900]}")
    return payload


def main() -> None:
    requests.post(f"{BASE}/data/d/reset", timeout=5).raise_for_status()
    client = MCPClient(lambda: stdio_client(StdioServerParameters(
        command=sys.executable, args=["-m", "guard.mcp_server"], cwd=str(ROOT))))
    run = uuid.uuid4().hex[:8]
    with client:
        print("工具：", [t.tool_name for t in client.list_tools_sync()])
        call(client, "check_budget")
        call(client, "fetch_paid_resource", url=f"{BASE}/data/a", purpose="mcp check", max_price=1, request_id=f"{run}-a")
        call(client, "fetch_paid_resource", url=f"{BASE}/data/a", purpose="mcp check", max_price=1, request_id=f"{run}-a")  # 幂等重放
        hold = call(client, "fetch_paid_resource", url=f"{BASE}/data/b", purpose="mcp check", max_price=10, request_id=f"{run}-b")
        call(client, "fetch_paid_resource", url=f"{BASE}/data/b", purpose="mcp check", max_price=5, request_id=f"{run}-b5")  # MAX_PRICE
        call(client, "fetch_paid_resource", url=f"{BASE}/data/d", purpose="mcp check", max_price=1, request_id=f"{run}-d")
        approval_id = (hold.get("result") or hold).get("approval_id")
        call(client, "approval_status", approval_id=approval_id)
        if "--wait" in sys.argv:
            print(f"\n现在去另一个终端运行：.venv/bin/python scripts/approve.py approve {approval_id}")
            call(client, "wait_for_approval", approval_id=approval_id, timeout_s=150)
    print(f"\n检查完毕：{len(SEEDS)} 个 seed 均未出现在任何工具返回值中")


if __name__ == "__main__":
    main()
