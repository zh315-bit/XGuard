"""对外的 Python 接口。内部实现在 guard/ 包里；这里只做一层稳定的门面。"""

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Union

from dotenv import load_dotenv

PathLike = Union[str, Path]


class Guard:
    """一个 agent 身份的支付守卫。

    - policy_path：策略文件（默认 AGENTGUARD_HOME / 当前目录下的 policy.yaml）
    - agent_id：幂等键的一部分，(agent_id, request_id) 只会付一次
    - env_file：钱包、RPC 等配置；不传就用 AGENTGUARD_HOME / 当前目录下的 .env

    注意：策略文件路径通过环境变量 POLICY_PATH 传给内部实现，同一进程里只能用一份策略。
    """

    def __init__(self, policy_path: Optional[PathLike] = None, agent_id: str = "default",
                 env_file: Optional[PathLike] = None) -> None:
        if env_file:
            load_dotenv(env_file)          # 必须在导入 guard 之前，guard.config 导入时就读取配置
        if policy_path:
            os.environ["POLICY_PATH"] = str(Path(policy_path).resolve())
        self.agent_id = agent_id

    def fetch_paid(self, url: str, purpose: str, request_id: str,
                   max_price_xrp: Optional[Union[str, int, float, Decimal]] = None) -> dict[str, Any]:
        """访问一个 x402 付费资源：守卫先看报价，按策略自动付款 / 转人工审批 / 拒绝。

        同一个 request_id 重复调用只会返回已有结果，不会再付款。返回值不含任何密钥。
        """
        from guard import views
        from guard.x402_gate import fetch_paid
        limit = Decimal(str(max_price_xrp)) if max_price_xrp is not None else None
        return views.present(fetch_paid(url, purpose, limit, agent_id=self.agent_id, request_id=request_id))

    def approval(self, approval_id: int) -> dict[str, Any]:
        """查询人工审批的结果：pending / approved（附内容）/ rejected / expired / delivery_rejected。"""
        from guard import approval_service, maintenance, views
        maintenance.sweep()
        return views.approval_for_agent(approval_service.view(approval_id))

    def budget(self) -> dict[str, Any]:
        """今日额度：自动付款日预算、已用 / 预留、单笔上限、硬上限、每日总上限（XRP）。"""
        from guard import budget, policy
        return budget.summary(policy.load_policy())
