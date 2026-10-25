"""R3 链上凭证：有效性判断、60 秒缓存、查询失败必须 HOLD。全部离线，用假客户端代替 XRPL 节点。"""

from datetime import datetime, timedelta, timezone

import pytest
from x402_xrpl.types import PaymentRequirements
from xrpl.models.response import Response, ResponseStatus
from xrpl.utils import datetime_to_ripple_time

from guard import credentials, evaluation
from guard.quote import Quote

SUBJECT, ISSUER, TYPE = "rSubject", "rIssuer", "AgentGuardVerifiedMerchant"


class FakeClient:
    def __init__(self, *, node=None, error=None, raises=None):
        self.node, self.error, self.raises, self.calls = node, error, raises, 0

    def request(self, _req):
        self.calls += 1
        if self.raises:
            raise self.raises
        if self.error:
            return Response(status=ResponseStatus.ERROR, result={"error": self.error})
        return Response(status=ResponseStatus.SUCCESS, result={"node": self.node})


@pytest.fixture
def use_client(monkeypatch):
    def install(client):
        monkeypatch.setattr(credentials, "get_client", lambda: client)
        return client
    return install


def ripple_time(delta):
    return datetime_to_ripple_time(datetime.now(timezone.utc) + delta)


@pytest.mark.parametrize("node, expected", [
    ({"Flags": credentials.LSF_ACCEPTED}, True),
    ({"Flags": 0}, False),                                                           # 签发了但商户没接受
    ({"Flags": credentials.LSF_ACCEPTED, "Expiration": ripple_time(timedelta(hours=1))}, True),
    ({"Flags": credentials.LSF_ACCEPTED, "Expiration": ripple_time(-timedelta(hours=1))}, False),  # 已过期
], ids=["accepted", "not_accepted", "not_expired", "expired"])
def test_validity(use_client, node, expected):
    use_client(FakeClient(node=node))
    assert credentials.is_verified(SUBJECT, ISSUER, TYPE) is expected


def test_missing_credential_is_not_verified(use_client):
    use_client(FakeClient(error="entryNotFound"))
    assert credentials.is_verified(SUBJECT, ISSUER, TYPE) is False


def test_result_cached_for_60s(use_client, monkeypatch):
    client = use_client(FakeClient(node={"Flags": credentials.LSF_ACCEPTED}))
    now = [1000.0]
    monkeypatch.setattr(credentials.time, "monotonic", lambda: now[0])

    credentials.is_verified(SUBJECT, ISSUER, TYPE)
    now[0] += 59
    credentials.is_verified(SUBJECT, ISSUER, TYPE)
    assert client.calls == 1
    now[0] += 2
    credentials.is_verified(SUBJECT, ISSUER, TYPE)
    assert client.calls == 2


@pytest.mark.parametrize("client", [
    FakeClient(raises=ConnectionError("timeout")),
    FakeClient(error="noNetwork"),
], ids=["network_exception", "node_error"])
def test_lookup_failure_raises_and_is_not_cached(use_client, client):
    use_client(client)
    for _ in range(2):
        with pytest.raises(credentials.CredentialLookupError):
            credentials.is_verified(SUBJECT, ISSUER, TYPE)
    assert client.calls == 2


def test_lookup_failure_makes_gate_hold_not_auto(use_client):
    """端到端到策略层：查询失败时，其他规则都满足的小额报价也只能 HOLD（R3）。"""
    use_client(FakeClient(raises=ConnectionError("timeout")))
    quote = Quote("http://x/data", PaymentRequirements(
        scheme="exact", network="xrpl:1", amount="10000", asset="XRP", pay_to=SUBJECT, max_timeout_seconds=600))

    decision = evaluation.decide(quote)

    assert (decision.verdict, decision.rule_id) == ("HOLD", "R3")
    assert "无法查询" in decision.reason
