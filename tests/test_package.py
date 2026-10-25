"""对外接口：agentguard.Guard 和 agentguard-mcp 入口（用本地假商户，离线）。"""

import shutil
import sys
from pathlib import Path

import pytest

import agentguard
from fakes import merchant, offline, terms  # noqa: F401


def test_guard_fetch_paid_and_idempotency(merchant, offline, tmp_path):
    guard = agentguard.Guard(policy_path=tmp_path / "policy.yaml", agent_id="sdk-user")

    first = guard.fetch_paid(merchant.base + "/data", purpose="sdk test", request_id="sdk-1")
    again = guard.fetch_paid(merchant.base + "/data", purpose="sdk test", request_id="sdk-1")

    assert first["status"] == "paid" and first["content"]["ok"] is True and first["tx_hash"]
    assert again["replay"] is True and again["tx_hash"] == first["tx_hash"]
    assert offline.calls == 1 and len(merchant.payment_headers) == 1
    assert "seed" not in repr(first).lower()


def test_guard_hold_and_approval_view(merchant, offline, tmp_path):
    merchant.default_terms = terms(8_000_000)
    guard = agentguard.Guard(policy_path=tmp_path / "policy.yaml", agent_id="sdk-user")

    held = guard.fetch_paid(merchant.base + "/data", purpose="big", request_id="sdk-2", max_price_xrp=10)

    assert held["status"] == "pending_approval" and held["rule_id"] == "R4"
    assert guard.approval(held["approval_id"])["status"] == "pending"
    assert guard.budget()["auto_limit_per_payment_xrp"] == "1"


def test_mcp_entry_point_lists_tools():
    """真实启动 agentguard-mcp 子进程（stdio），用 Strands 的 MCPClient 列出工具。"""
    from mcp import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient
    exe = shutil.which("agentguard-mcp", path=str(Path(sys.executable).parent))
    if exe is None:
        pytest.skip("agentguard 没有以 pip install -e . 安装")
    client = MCPClient(lambda: stdio_client(StdioServerParameters(command=exe, args=["--agent-id", "mcp-test"])))
    with client:
        names = sorted(t.tool_name for t in client.list_tools_sync())
    assert names == ["approval_status", "check_budget", "fetch_paid_resource", "list_data_sources", "wait_for_approval"]
