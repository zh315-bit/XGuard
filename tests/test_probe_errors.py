"""探测阶段的非 402 响应：只有 2xx 算免费资源；4xx / 5xx / 3xx / 连接失败 / 超时都是明确错误，不付款，内容不交给 agent。"""

import socket

import pytest

from guard import audit, payments, views, x402_gate
from guard import state_machine as sm
from fakes import fetch, merchant, offline  # noqa: F401


def assert_failed_not_paid(r, merchant, offline, *needles):
    assert r.state == sm.FAILED and (r.verdict, r.rule_id) == ("ERROR", "PROBE") and not r.paid
    assert offline.calls == 0 and merchant.payment_headers == []           # 没签名、没付款
    assert payments.get(r.payment_id)["result_json"] is None
    agent_view = views.present(r)
    assert agent_view["status"] == "fetch_failed" and "content" not in agent_view
    assert "MUST NOT REACH" not in str(agent_view)                         # 页面内容不交给 agent
    for needle in needles:
        assert needle in agent_view["error"]
    assert audit.get_decision(r.decision_id)["status"] == "fetch_failed"   # 审计记为失败


@pytest.mark.parametrize("code, needles", [
    (404, ("HTTP 404", "Not Found")),
    (500, ("HTTP 500", "merchant-side error")),
    (302, ("HTTP 302", "not followed", "elsewhere.example")),
], ids=["404", "500", "302"])
def test_http_errors(merchant, offline, code, needles):
    r = fetch(merchant, f"/status/{code}")
    assert_failed_not_paid(r, merchant, offline, *needles)


def test_connection_failure(merchant, offline):
    with socket.socket() as s:                                             # 拿一个没人监听的端口
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    merchant_base, merchant.base = merchant.base, f"http://127.0.0.1:{port}"
    r = fetch(merchant, "/data")
    merchant.base = merchant_base
    assert_failed_not_paid(r, merchant, offline, "Could not connect")


def test_timeout(merchant, offline, monkeypatch):
    monkeypatch.setattr(x402_gate, "PROBE_TIMEOUT", 0.3)
    r = fetch(merchant, "/slow")
    assert_failed_not_paid(r, merchant, offline, "timed out")


def test_2xx_is_free(merchant, offline):
    r = fetch(merchant, "/status/200")
    assert (r.state, r.verdict) == (sm.RESOURCE_DELIVERED, "FREE")
    assert views.present(r)["status"] == "free" and offline.calls == 0
