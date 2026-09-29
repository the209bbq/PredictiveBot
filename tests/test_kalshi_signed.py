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


def test_shutdown_scopes_to_bot_orders_then_alerts(monkeypatch):
    cfg = load_config()
    client = KalshiClient(cfg)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    calls = {"cancel_all": 0, "cancel_bot": 0, "batch": 0, "list_bot": 0}

    def cancel_all(*, confirm_demo):
        calls["cancel_all"] += 1
        return {}

    def cancel_bot(*, confirm_demo):
        calls["cancel_bot"] += 1
        return {"cancelled": 1}

    def list_bot(*, status="resting", confirm_demo=False):
        calls["list_bot"] += 1
        return [{"order_id": "abc", "ticker": "KXDEMO", "client_order_id": "pmbot-1"}]

    def batch(orders, *, confirm_demo):
        calls["batch"] += 1
        assert orders[0]["order_id"] == "abc"
        return {}

    client.cancel_all_demo_orders = cancel_all  # type: ignore[method-assign]
    client.cancel_bot_demo_orders = cancel_bot  # type: ignore[method-assign]
    client.list_bot_demo_orders = list_bot  # type: ignore[method-assign]
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
    assert calls["cancel_bot"] == 1
    assert calls["cancel_all"] == 0
    assert calls["batch"] == 1


def test_shutdown_ignores_foreign_account_orders(monkeypatch):
    cfg = load_config()
    client = KalshiClient(cfg)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    calls = {"cancel_all": 0}

    def cancel_all(*, confirm_demo):
        calls["cancel_all"] += 1
        return {}

    client.cancel_all_demo_orders = cancel_all  # type: ignore[method-assign]
    client.list_demo_orders = lambda **k: [
        {"order_id": "someone-else", "ticker": "KXDEMO", "client_order_id": "manual-1"}
    ]  # type: ignore[method-assign]
    client.batch_cancel_demo_orders = lambda *a, **k: {}  # type: ignore[method-assign]
    try:
        assert client.shutdown_demo_orders(confirm_demo=True) == []
    finally:
        client.close()
    assert calls["cancel_all"] == 0


def test_emergency_shutdown_cancels_all_account_orders(monkeypatch):
    cfg = load_config()
    client = KalshiClient(cfg)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    calls = {"cancel_all": 0}

    def cancel_all(*, confirm_demo):
        calls["cancel_all"] += 1
        return {}

    client.cancel_all_demo_orders = cancel_all  # type: ignore[method-assign]
    client.list_demo_orders = lambda **k: []  # type: ignore[method-assign]
    try:
        assert client.shutdown_demo_orders(confirm_demo=True, emergency_all=True) == []
    finally:
        client.close()
    assert calls["cancel_all"] == 1


def test_is_bot_order_uses_prefix_or_tracked_id():
    cfg = load_config()
    client = KalshiClient(cfg)
    try:
        assert client.is_bot_order({"client_order_id": "pmbot-abc", "order_id": "x"})
        assert not client.is_bot_order({"client_order_id": "manual-1", "order_id": "y"})
        client._bot_order_ids.add("y")
        assert client.is_bot_order({"client_order_id": "manual-1", "order_id": "y"})
    finally:
        client.close()


def test_shutdown_clean_when_empty(monkeypatch):
    cfg = load_config()
    client = KalshiClient(cfg)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    client.cancel_bot_demo_orders = lambda **k: {"cancelled": 0}  # type: ignore[method-assign]
    client.list_bot_demo_orders = lambda **k: []  # type: ignore[method-assign]
    client.cancel_all_demo_orders = lambda **k: (_ for _ in ()).throw(AssertionError("cancel_all"))  # type: ignore[method-assign]
    try:
        assert client.shutdown_demo_orders(confirm_demo=True) == []
    finally:
        client.close()


def test_place_demo_order_tags_client_order_id(monkeypatch):
    cfg = load_config()
    client = KalshiClient(cfg)
    captured: dict = {}

    def signed(method, path, *, confirm_demo, body=None, params=None):
        captured["body"] = body
        return {"order": {"order_id": "oid-1", "client_order_id": body["client_order_id"]}}

    client.signed_demo = signed  # type: ignore[method-assign]
    try:
        out = client.place_demo_order(
            ticker="KXDEMO",
            side="bid",
            price="0.01",
            count="1",
            confirm_demo=True,
        )
    finally:
        client.close()
    assert captured["body"]["client_order_id"].startswith("pmbot-")
    assert captured["body"]["side"] == "bid"
    assert captured["body"]["post_only"] is True
    assert "oid-1" in client._bot_order_ids
    assert out["order"]["order_id"] == "oid-1"


def test_last_trade_helper_decimal():
    cfg = load_config()
    client = KalshiClient(cfg)
    client._get = lambda *a, **k: {"trades": [{"yes_price_dollars": "0.33"}]}  # type: ignore[method-assign]
    try:
        assert client.last_trade("T") == Decimal("0.33")
    finally:
        client.close()
