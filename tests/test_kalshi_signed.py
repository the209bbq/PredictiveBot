import time
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from polymarket_bot.config import load_config
from polymarket_bot.exchanges.kalshi import KalshiClient, signed_request
from polymarket_bot.guard import DemoOrderError


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self.content = b"{}" if payload is not None or status == 200 else (text.encode() if text else b"")
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, *args, **kwargs):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


def test_signed_request_retries_503_then_succeeds(monkeypatch):
    key = Ed25519PrivateKey.generate()
    fake = _Client([_Resp(503, "service_unavailable"), _Resp(200, payload={"ok": True})])
    monkeypatch.setattr("polymarket_bot.exchanges.kalshi.httpx.Client", lambda **k: fake)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    out = signed_request(
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        method="DELETE",
        path="/portfolio/events/orders",
        key_id="demo-key",
        private_key=key,
        max_retries=4,
    )
    assert out == {"ok": True}
    assert fake.calls == 2


def test_signed_request_retries_then_raises(monkeypatch):
    key = Ed25519PrivateKey.generate()
    fake = _Client([_Resp(503, "service_unavailable")])
    monkeypatch.setattr("polymarket_bot.exchanges.kalshi.httpx.Client", lambda **k: fake)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    try:
        signed_request(
            base_url="https://demo-api.kalshi.co/trade-api/v2",
            method="DELETE",
            path="/portfolio/events/orders",
            key_id="demo-key",
            private_key=key,
            max_retries=3,
        )
        raise AssertionError("should have raised")
    except DemoOrderError as exc:
        assert "after retries" in str(exc)
        assert "503" in str(exc)
    assert fake.calls == 3


def test_shutdown_cancel_all_then_batch_then_alert(monkeypatch):
    cfg = load_config()
    client = KalshiClient(cfg)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    calls = {"cancel_all": 0, "batch": 0, "list": 0}

    def cancel_all(*, confirm_demo):
        calls["cancel_all"] += 1
        return {}

    def list_orders(*, status="resting", confirm_demo=False):
        calls["list"] += 1
        if calls["list"] == 1:
            return [{"order_id": "abc", "ticker": "KXDEMO"}]
        return [{"order_id": "abc", "ticker": "KXDEMO"}]

    def batch(orders, *, confirm_demo):
        calls["batch"] += 1
        assert orders[0]["order_id"] == "abc"
        return {}

    client.cancel_all_demo_orders = cancel_all  # type: ignore[method-assign]
    client.list_demo_orders = list_orders  # type: ignore[method-assign]
    client.batch_cancel_demo_orders = batch  # type: ignore[method-assign]
    try:
        try:
            client.shutdown_demo_orders(confirm_demo=True)
            raise AssertionError("should have raised")
        except DemoOrderError as exc:
            assert "ALERT" in str(exc)
            assert "abc" in str(exc)
    finally:
        client.close()
    assert calls["cancel_all"] == 1
    assert calls["batch"] == 1


def test_shutdown_clean_when_empty(monkeypatch):
    cfg = load_config()
    client = KalshiClient(cfg)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    client.cancel_all_demo_orders = lambda **k: {}  # type: ignore[method-assign]
    client.list_demo_orders = lambda **k: []  # type: ignore[method-assign]
    try:
        assert client.shutdown_demo_orders(confirm_demo=True) == []
    finally:
        client.close()


def test_last_trade_helper_decimal():
    cfg = load_config()
    client = KalshiClient(cfg)
    client._get = lambda *a, **k: {"trades": [{"yes_price_dollars": "0.33"}]}  # type: ignore[method-assign]
    try:
        assert client.last_trade("T") == Decimal("0.33")
    finally:
        client.close()
