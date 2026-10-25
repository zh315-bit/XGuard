"""演示 agent：用 Strands Agents SDK，通过 MCP 接入 AgentGuard，买四份数据写 XRPL 生态周报。

运行（需要商户 8000、守卫 API 8100 已启动，.env 里配好 ANTHROPIC_API_KEY）：
  .venv/bin/python agent/agent.py
B 进入审批后，在另一个终端：.venv/bin/python scripts/approve.py list / approve <id>

安全边界：这个进程不 import guard、不把 .env 整体加载进环境变量，
只按名字读取模型配置；钱包 seed 只存在于它拉起的 MCP server 子进程里。
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import dotenv_values
from mcp import StdioServerParameters, stdio_client
from mcp.client.stdio import get_default_environment
from strands import Agent
from strands.event_loop._retry import ModelRetryStrategy
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry, MessageAddedEvent
from strands.tools.mcp import MCPClient
from strands.types.exceptions import ModelThrottledException

sys.path.insert(0, str(Path(__file__).resolve().parent))
import presets  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 只取 agent 需要的键；环境变量优先于 .env，方便临时切换
_ALLOWED_KEYS = ("ANTHROPIC_API_KEY", "AGENT_MODEL_PROVIDER", "AGENT_MODEL_ID",
                 "BEDROCK_MODEL_ID", "AWS_REGION", "MERCHANT_BASE_URL", "GUARD_API_URL")
_file = dotenv_values(ROOT / ".env")
CONFIG = {k: os.getenv(k) or _file.get(k) for k in _ALLOWED_KEYS}

SYSTEM_PROMPT = """你是一个研究 agent，帮用户完成数据研究任务（例如撰写 XRPL 生态周报）。
你没有钱包和私钥，所有付费数据只能通过 AgentGuard 的 fetch_paid_resource 工具购买。
不知道去哪买数据时，先用 list_data_sources 查看可购买的数据目录（URL、描述、参考价格），自己决定买哪些。
花钱要克制：只买完成任务真正需要的数据，不要重复购买同一份数据。
每次购买都要给一个唯一的 request_id（例如 weekly-report-data-a）；重试同一次购买必须复用同一个 request_id。
根据返回的 status 处理：
- paid：数据已到手，直接使用。
- pending_approval：需要人工审批。把 reason、risk_summary 和 approver_command 告诉用户（审批单约 2 分钟内有效），
  然后调用 wait_for_approval 等待（timeout_s 用 150）。不要重复调用 fetch_paid_resource。
  仍是 pending 就再等一次；approved 就使用 content；rejected 或 expired 就说明原因并跳过这份数据。
- denied：守卫拒绝付款。不要重试同一个 URL；向用户说明被拒的原因（引用守卫给的 reason），然后跳过这份数据。
- delivery_rejected / refunded：escrow 验货不通过，钱不会付给商户（到期退回金库）。引用 reason 向用户说明，换别的数据源。
- settlement_unknown：付款结果未知。不要换 request_id 重买，用同一个 request_id 再查一次，仍未知就如实告诉用户。
最后基于实际拿到的数据给出结果（周报或研究结论），并列出每份数据的来源、付款结果和交易哈希；没拿到的数据要注明原因，不要编造。"""


def task_prompt() -> str:
    """任务文本：网页发起的任务通过 AGENTGUARD_TASK 传入；否则用默认的周报任务。
    任务文本只是 agent 的输入，守卫的判定完全不看它。"""
    task = os.getenv("AGENTGUARD_TASK")
    if task:
        return task[:presets.MAX_TASK_CHARS]
    return presets.weekly_report(CONFIG["MERCHANT_BASE_URL"] or "http://127.0.0.1:8000")


# ---- 模型：可配置，现在用 Anthropic API，之后切到 Bedrock 只改 .env ----

LLM_TIMEOUT_SECONDS = 30   # 单次请求（流式时是两段数据之间）最多等 30 秒，避免连接半断时一直挂着
LLM_MAX_RETRIES = 2        # 暂时性错误最多重试 2 次（共 3 次），之后任务中止


def llm_error_kind(exc: BaseException) -> Optional[str]:
    """暂时性的 LLM 错误返回类型（timeout / network / rate_limit / overloaded / server），否则 None。"""
    import anthropic
    import httpx
    for e in _causes(exc):
        if isinstance(e, (anthropic.APITimeoutError, httpx.TimeoutException)):
            return "timeout"
        if isinstance(e, (anthropic.APIConnectionError, httpx.TransportError)):
            return "network"
        if isinstance(e, (anthropic.RateLimitError, ModelThrottledException)):
            return "rate_limit"
        if isinstance(e, anthropic.APIStatusError):
            if e.status_code == 429:
                return "rate_limit"
            if e.status_code == 529 or "overloaded" in str(e).lower():
                return "overloaded"
            if e.status_code >= 500:
                return "server"
    return None


def llm_failure_kind(exc: BaseException) -> Optional[str]:
    """任何 LLM 调用失败（含鉴权、额度这类不重试的）返回类型；不是 LLM 的错误返回 None。"""
    import anthropic
    kind = llm_error_kind(exc)
    if kind:
        return kind
    for e in _causes(exc):
        if isinstance(e, anthropic.APIStatusError):
            if e.status_code in (401, 403):
                return "auth"
            text = str(e).lower()
            return "quota" if "credit" in text or "billing" in text else "bad_request"
    return None


def _causes(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


class TransientLLMRetry(ModelRetryStrategy):
    """Strands 默认只重试限速；这里把超时、网络中断、过载、5xx 也算上，次数有限。"""

    def __init__(self) -> None:
        super().__init__(max_attempts=LLM_MAX_RETRIES + 1, initial_delay=2, max_delay=8)

    def is_retryable(self, exception: Exception) -> bool:
        retryable = llm_error_kind(exception) is not None
        if retryable:
            print(f"[llm] 调用失败（{llm_error_kind(exception)}：{exception}），稍后重试", file=sys.stderr)
        return retryable


def build_model():
    provider = (CONFIG["AGENT_MODEL_PROVIDER"] or "anthropic").lower()
    if provider == "anthropic":
        from strands.models.anthropic import AnthropicModel
        if not CONFIG["ANTHROPIC_API_KEY"]:
            sys.exit("缺少 ANTHROPIC_API_KEY：写进 .env 或 export 后再运行")
        return AnthropicModel(
            # 重试交给 TransientLLMRetry 统一控制（流式中途断开也能重试），SDK 自己不再重试
            client_args={"api_key": CONFIG["ANTHROPIC_API_KEY"], "timeout": LLM_TIMEOUT_SECONDS, "max_retries": 0},
            model_id=CONFIG["AGENT_MODEL_ID"] or "claude-opus-5-5",
            max_tokens=16000,
        )
    if provider == "bedrock":
        from strands.models import BedrockModel
        # Bedrock 的 model id 带 anthropic. 前缀；部分区域需要用推理配置文件 id（如 us.anthropic....），以控制台为准
        return BedrockModel(
            model_id=CONFIG["BEDROCK_MODEL_ID"] or "anthropic.claude-opus-5-5",
            region_name=CONFIG["AWS_REGION"] or "us-east-1",
        )
    sys.exit(f"不支持的 AGENT_MODEL_PROVIDER：{provider}（可选 anthropic / bedrock）")


def guard_mcp_client(agent_id: Optional[str] = None) -> MCPClient:
    """以子进程方式拉起 guard/mcp_server.py（stdio）。子进程自己从 .env 读钱包配置。
    agent_id 由这里（代码）决定，不是 LLM 能改的：每个网页任务用自己的 agent_id，幂等记录互不串用。"""
    env = {**get_default_environment(), **({"AGENT_ID": agent_id} if agent_id else {})}
    return MCPClient(lambda: stdio_client(StdioServerParameters(
        command=sys.executable, args=["-m", "guard.mcp_server"], cwd=str(ROOT), env=env)))


# ---- demo 输出：每次工具调用和结果都打印出来 ----

class ToolTrace(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def before(self, event: BeforeToolCallEvent) -> None:
        tool = event.tool_use
        print(f"\n\n┌─ 工具调用 {tool['name']}")
        print(f"│  参数 {json.dumps(tool['input'], ensure_ascii=False)}")

    def after(self, event: AfterToolCallEvent) -> None:
        if isinstance(event.result, Exception):
            print(f"└─ 异常 {event.result}\n")
            return
        payload = _tool_payload(event.result)
        keys = [k for k in ("verdict", "rule_id", "status", "reason", "approval_id", "tx_hash", "explorer_url")
                if isinstance(payload, dict) and payload.get(k) is not None]
        if keys:
            for key in keys:
                print(f"│  {key:<12} {payload[key]}")
            if payload.get("content") is not None:
                print(f"│  content      {json.dumps(payload['content'], ensure_ascii=False)[:160]}")
        else:
            print(f"│  {json.dumps(payload, ensure_ascii=False, default=str)[:400]}")
        print(f"└─ {event.result.get('status')}\n")


class MissionReporter(HookProvider):
    """把每一步上报给守卫 API（控制台 Mission 页面展示）。只是展示，上报失败不影响 agent。"""

    def __init__(self, task: str, mission_id: Optional[int] = None) -> None:
        """mission_id：网页发起的任务由守卫 API 先建好记录并传进来；命令行运行时自己注册一个。"""
        self.api = CONFIG["GUARD_API_URL"] or "http://127.0.0.1:8100"
        self.mission_id = mission_id
        self.results: list[tuple[str, Any, Any]] = []   # (工具名, 参数, 结果)：任务中止时生成已完成步骤摘要
        if mission_id is not None:
            return
        try:
            resp = requests.post(f"{self.api}/api/missions/register", json={"task": task, "pid": os.getpid()},
                                 timeout=5)
            if resp.status_code == 409:
                print(f"[mission] 控制台上有任务正在运行，这次不在 Mission 页面显示：{resp.json().get('detail')}",
                      file=sys.stderr)
                return
            self.mission_id = resp.json()["id"]
        except requests.RequestException as exc:
            print(f"[mission] 守卫 API 不可达，控制台不会显示任务进度：{exc}", file=sys.stderr)

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)
        registry.add_callback(MessageAddedEvent, self.message)

    def before(self, event: BeforeToolCallEvent) -> None:
        self._post("events", kind="tool_call", name=event.tool_use["name"], payload=event.tool_use["input"])

    def after(self, event: AfterToolCallEvent) -> None:
        payload = {"error": str(event.result)} if isinstance(event.result, Exception) else _tool_payload(event.result)
        self.results.append((event.tool_use["name"], event.tool_use["input"], payload))
        self._post("events", kind="tool_result", name=event.tool_use["name"], payload=payload)

    def message(self, event: MessageAddedEvent) -> None:
        if event.message.get("role") != "assistant":
            return
        text = "".join(block.get("text", "") for block in event.message.get("content", [])).strip()
        if text:
            self._post("events", kind="text", payload=text[:2000])

    def finish(self, status: str, report: Optional[str]) -> None:
        self._post("finish", status=status, report=report)

    def llm_status(self, ok: bool, kind: Optional[str] = None, detail: Optional[str] = None) -> None:
        """控制台的 LLM 状态灯。不依赖任务记录：命令行运行时也上报。"""
        try:
            requests.post(f"{self.api}/api/llm/events", json={"source": "agent", "ok": ok, "kind": kind,
                                                               "detail": (detail or "")[:300]}, timeout=5)
        except requests.RequestException:
            pass

    def progress_summary(self) -> str:
        """已完成步骤的摘要（Markdown），由工具结果生成，不需要 LLM。"""
        lines = []
        for name, args, payload in self.results:
            if name not in ("fetch_paid_resource", "wait_for_approval"):
                continue
            p = payload if isinstance(payload, dict) else {}
            what = args.get("url") or f"审批单 #{args.get('approval_id')}"
            status = p.get("status") or ("出错" if "error" in p else "—")
            extra = p.get("reason") or (f"交易 {p['tx_hash'][:12]}…" if p.get("tx_hash") else "")
            lines.append(f"- `{what}`：{status}{('，' + str(extra)[:160]) if extra else ''}")
        return "\n".join(lines) or "- 还没有完成任何购买"

    def _post(self, path: str, **body: Any) -> None:
        if self.mission_id is None:
            return
        try:
            requests.post(f"{self.api}/api/missions/{self.mission_id}/{path}", json=body, timeout=5)
        except requests.RequestException:
            pass


def _tool_payload(result: dict) -> Any:
    """MCP 工具结果在 Strands 里是 content 列表（text 或 json）；取出第一项并尽量解析成 dict。"""
    for item in result.get("content", []):
        if "json" in item:
            return item["json"]
        if "text" in item:
            try:
                return json.loads(item["text"])
            except ValueError:
                return item["text"]
    return result


def main() -> None:
    model = build_model()
    mission_id = int(os.environ["AGENTGUARD_MISSION_ID"]) if os.getenv("AGENTGUARD_MISSION_ID") else None
    mcp_client = guard_mcp_client(f"mission-{mission_id}" if mission_id else None)
    with mcp_client:
        tools = mcp_client.list_tools_sync()
        print("AgentGuard 工具：", ", ".join(t.tool_name for t in tools))
        task = task_prompt()
        reporter = MissionReporter(task, mission_id)
        agent = Agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT, hooks=[ToolTrace(), reporter],
                      retry_strategy=TransientLLMRetry())
        try:
            result = agent(task)
        except Exception as exc:
            kind = llm_failure_kind(exc)
            if kind is None:
                reporter.finish("failed", None)
                raise
            # LLM 重试后仍不可用：结束任务，不无限等待；付款状态以守卫记录为准
            reporter.llm_status(False, kind, str(exc))
            reporter.finish("llm_unavailable", f"**LLM 不可用，任务中止**（{kind}：{str(exc)[:200]}）\n\n"
                                               f"已完成的步骤：\n{reporter.progress_summary()}")
            print(f"\nLLM 不可用，任务中止：{kind}：{exc}", file=sys.stderr)
            sys.exit(2)
        reporter.llm_status(True)
        # 最后一段助手文字就是周报
        reporter.finish("done", str(result).strip())
    print()


if __name__ == "__main__":
    main()
