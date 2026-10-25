"""LLM 调用的超时与有限重试；最终失败时守卫保守处理、agent 中止任务；控制台状态灯。全部离线，用假客户端 / 假模型。"""

import sys
from pathlib import Path

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from guard import delivery_verifier, llm

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def timeout_error():
    return anthropic.APITimeoutError(request=REQUEST)


def status_error(code, message="error"):
    return anthropic.APIStatusError(message, response=httpx.Response(code, request=REQUEST), body=None)


class FakeAnthropic:
    """按顺序抛出 / 返回 outcomes 里的东西；记录调用次数和超时设置。"""
    outcomes: list = []
    calls = 0
    init_kwargs: dict = {}

    def __init__(self, **kwargs):
        FakeAnthropic.init_kwargs = kwargs
        self.beta = self
        self.messages = self

    def stream(self, **kwargs):
        FakeAnthropic.calls += 1
        return FakeStream(FakeAnthropic.outcomes.pop(0))


class FakeStream:
    """流式调用：outcome 是异常就在迭代时抛出（模拟中途断开），否则作为最终消息返回。"""

    def __init__(self, outcome, chunks=1):
        self.outcome, self.chunks = outcome, chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for _ in range(self.chunks):
            yield {"type": "content_block_delta"}
        if isinstance(self.outcome, BaseException):
            raise self.outcome

    def get_final_message(self):
        return self.outcome


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    FakeAnthropic.calls, FakeAnthropic.outcomes = 0, []
    return FakeAnthropic


def test_transient_errors_are_retried_twice_then_succeed(fake_api):
    fake_api.outcomes = [timeout_error(), status_error(529, "overloaded"), "OK"]
    assert llm.create_message("delivery_verifier", model="m") == "OK"
    assert fake_api.calls == 3
    assert fake_api.init_kwargs["timeout"] == 30 and fake_api.init_kwargs["max_retries"] == 0
    assert llm.recent()[0]["ok"] is True


def test_gives_up_after_two_retries(fake_api):
    fake_api.outcomes = [timeout_error(), timeout_error(), timeout_error(), "never"]
    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.create_message("delivery_verifier", model="m")
    assert fake_api.calls == 3 and exc.value.kind == "timeout" and "retried 2 time(s)" in str(exc.value)
    assert (llm.recent()[0]["ok"], llm.recent()[0]["kind"]) == (False, "timeout")


@pytest.mark.parametrize("error, kind", [(status_error(401), "auth"), (status_error(400, "credit balance is too low"), "quota"),
                                         (status_error(400), "bad_request")])
def test_permanent_errors_are_not_retried(fake_api, error, kind):
    fake_api.outcomes = [error, "never"]
    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.create_message("risk_explainer", model="m")
    assert fake_api.calls == 1 and exc.value.kind == kind


@pytest.mark.parametrize("error, kind", [
    (anthropic.APIConnectionError(request=REQUEST), "network"), (httpx.ReadError("reset"), "network"),
    (status_error(429), "rate_limit"), (status_error(503), "server")])
def test_classify(error, kind):
    assert llm.classify(error) == (kind, True)


def test_verifier_failure_reason_names_the_cause(fake_api, monkeypatch):
    monkeypatch.setenv("DELIVERY_VERIFIER", "llm")
    fake_api.outcomes = [timeout_error()] * 3
    delivery = {"dataset": "x", "week": "w", "unit": "XRP",
                "daily": [{"date": f"2026-10-{d:02d}", "volume_xrp": d} for d in range(1, 8)]}
    result = delivery_verifier.verify("desc", {"required": ["dataset"]}, delivery)
    assert result["verdict"] == "fail" and "request timed out, retried 2 time(s)" in result["reason"]


# ---- 状态灯 ----

def test_status_endpoint(fake_api, monkeypatch):
    from guard.api import app
    monkeypatch.setattr(llm, "probe", lambda: {"ok": True, "latency_ms": 5, "kind_text": None})
    client = TestClient(app)
    assert client.get("/api/llm/status").json()["ok"] is True

    assert client.post("/api/llm/events", json={"ok": False, "kind": "network", "detail": "ReadError"}).status_code == 200
    status = client.get("/api/llm/status").json()
    assert status["ok"] is False and "agent" in status["reason"] and "network connection dropped" in status["reason"]

    client.post("/api/llm/events", json={"ok": True})
    assert client.get("/api/llm/status").json()["ok"] is True


def test_successful_probe_after_a_failure_clears_the_light(fake_api, monkeypatch):
    from guard.api import app
    client = TestClient(app)
    client.post("/api/llm/events", json={"ok": False, "kind": "timeout", "detail": ""})
    failed_at = llm.recent()[0]["ts"]

    # 探测发生在失败之前（缓存）：仍然亮红灯
    monkeypatch.setattr(llm, "probe", lambda: {"ok": True, "latency_ms": 5, "kind_text": None, "ts": failed_at - 1})
    assert client.get("/api/llm/status").json()["ok"] is False
    # 失败之后探测成功：API 已恢复，状态灯转绿，失败记录还在
    monkeypatch.setattr(llm, "probe", lambda: {"ok": True, "latency_ms": 5, "kind_text": None, "ts": failed_at + 1})
    status = client.get("/api/llm/status").json()
    assert status["ok"] is True and status["reason"] is None
    assert status["calls"][0]["ok"] is False and status["calls"][0]["source"] == "agent"
    # 探测本身失败：照样红灯
    monkeypatch.setattr(llm, "probe", lambda: {"ok": False, "kind_text": "request timed out", "ts": failed_at + 1})
    assert client.get("/api/llm/status").json()["ok"] is False


def test_probe_reports_unavailable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    llm._probe_cache.clear()
    p = llm.probe()
    llm._probe_cache.clear()
    assert p["ok"] is False and p["kind"] == "not_configured"


def test_mission_can_finish_as_llm_unavailable():
    from guard import missions
    from guard.api import app
    mid = missions.create("t")
    resp = TestClient(app).post(f"/api/missions/{mid}/finish", json={"status": "llm_unavailable", "report": "**LLM 不可用，任务中止**"})
    assert resp.status_code == 200 and missions.get(mid)["status"] == "llm_unavailable"


# ---- agent：Strands 的重试策略 ----

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"


def load_agent_module():
    """按文件路径加载 agent/agent.py（名字 agent 会和 agent 包冲突）。"""
    import importlib.util
    if "agentguard_demo_agent" not in sys.modules:
        sys.path.insert(0, str(AGENT_DIR))          # agent.py 自己 import presets
        spec = importlib.util.spec_from_file_location("agentguard_demo_agent", AGENT_DIR / "agent.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["agentguard_demo_agent"] = module
        spec.loader.exec_module(module)
    return sys.modules["agentguard_demo_agent"]


from strands.models import Model  # noqa: E402


class FlakyModel(Model):
    """假 Strands 模型：前 n 次在流式中途断开（httpx.ReadError），之后正常回答。"""

    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    def update_config(self, **kwargs):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield

    async def stream(self, *args, **kwargs):
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.calls <= self.failures:
            raise httpx.ReadError("connection reset mid-stream")
        yield {"contentBlockDelta": {"delta": {"text": "周报写好了"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def make_agent(model):
    agent_module = load_agent_module()
    from strands import Agent
    return agent_module, Agent(model=model, retry_strategy=agent_module.TransientLLMRetry(), callback_handler=None)


@pytest.fixture
def no_backoff(monkeypatch):
    import asyncio
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s, *a, **k: real_sleep(0))


def test_agent_retries_mid_stream_disconnect(no_backoff):
    model = FlakyModel(failures=4)
    agent_module, agent = make_agent(model)
    assert agent_module.LLM_MAX_RETRIES == 4
    assert "周报写好了" in str(agent("写周报")) and model.calls == 5


def test_agent_gives_up_after_max_retries(no_backoff):
    model = FlakyModel(failures=5)
    agent_module, agent = make_agent(model)
    with pytest.raises(Exception) as exc:
        agent("写周报")
    assert model.calls == 5 and agent_module.llm_failure_kind(exc.value) == "network"


def test_agent_progress_summary():
    agent_module = load_agent_module()
    reporter = agent_module.MissionReporter.__new__(agent_module.MissionReporter)
    reporter.results = [
        ("list_data_sources", {}, {"sources": []}),
        ("fetch_paid_resource", {"url": "http://m/data/a"}, {"status": "paid", "tx_hash": "ABCDEF1234567890"}),
        ("wait_for_approval", {"approval_id": 29}, {"status": "delivery_rejected", "reason": "验货不通过"}),
    ]
    summary = reporter.progress_summary()
    assert "`http://m/data/a`: paid, tx ABCDEF123456…" in summary
    assert "`approval #29`: delivery_rejected, 验货不通过" in summary and "list_data_sources" not in summary


def test_slow_but_alive_stream_is_not_a_timeout(fake_api, monkeypatch):
    """模型生成慢但一直有数据：不算超时，不重试；超过总时长上限才放弃。"""
    clock = iter(range(0, 1000, 20))                      # 每次读时钟过去 20 秒
    monkeypatch.setattr(llm.time, "monotonic", lambda: next(clock))
    fake_api.outcomes = ["OK"]
    monkeypatch.setattr(FakeAnthropic, "stream", lambda self, **kw: (setattr(FakeAnthropic, "calls", FakeAnthropic.calls + 1),
                                                                      FakeStream(fake_api.outcomes[0], chunks=10))[1])
    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.create_message("risk_explainer", model="m")
    assert exc.value.kind == "too_slow" and fake_api.calls == 1


def test_stream_with_steady_chunks_succeeds(fake_api):
    fake_api.outcomes = ["OK"]
    assert llm.create_message("risk_explainer", model="m") == "OK" and fake_api.calls == 1
