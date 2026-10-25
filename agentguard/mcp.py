"""MCP server 启动入口：`agentguard-mcp --agent-id my-agent [--policy policy.yaml] [--env-file .env]`。

agent 通过 MCP 拿到 4 个工具：fetch_paid_resource / check_budget / approval_status / wait_for_approval。
agent_id 由启动参数决定，agent 自己不能改。传输方式 stdio。
"""

import argparse
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def serve(agent_id: Optional[str] = None, policy_path: Optional[str] = None, env_file: Optional[str] = None) -> None:
    if env_file:
        load_dotenv(env_file)
    if agent_id:
        os.environ["AGENT_ID"] = agent_id
    if policy_path:
        os.environ["POLICY_PATH"] = str(Path(policy_path).resolve())
    from guard.mcp_server import server     # 在设置好环境变量之后再导入
    server.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentGuard MCP server（stdio）")
    parser.add_argument("--agent-id", help="这个 agent 的身份（幂等键的一部分）")
    parser.add_argument("--policy", help="策略文件路径（默认 policy.yaml）")
    parser.add_argument("--env-file", help="配置文件路径（默认 .env）")
    args = parser.parse_args()
    serve(args.agent_id, args.policy, args.env_file)


if __name__ == "__main__":
    main()
