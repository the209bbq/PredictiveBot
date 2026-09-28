from polymarket_bot.guard import (
    DemoOrderError,
    assert_kalshi_demo_orders_allowed,
    is_kalshi_demo_url,
    is_kalshi_production_url,
)


def test_url_classification():
    assert is_kalshi_demo_url("https://external-api.demo.kalshi.co/trade-api/v2")
    assert is_kalshi_production_url("https://external-api.kalshi.com/trade-api/v2")
    assert is_kalshi_production_url("https://api.elections.kalshi.com/trade-api/v2")
    assert not is_kalshi_production_url("https://external-api.demo.kalshi.co/trade-api/v2")


def test_demo_orders_refuse_production_url():
    try:
        assert_kalshi_demo_orders_allowed(
            enabled=True,
            base_url="https://external-api.kalshi.com/trade-api/v2",
        )
        raise AssertionError("should have raised")
    except DemoOrderError as exc:
        assert "production" in str(exc).lower()


def test_demo_orders_refuse_when_disabled():
    try:
        assert_kalshi_demo_orders_allowed(
            enabled=False,
            base_url="https://external-api.demo.kalshi.co/trade-api/v2",
        )
        raise AssertionError("should have raised")
    except DemoOrderError:
        pass


def test_demo_orders_allow_demo_host():
    assert_kalshi_demo_orders_allowed(
        enabled=True,
        base_url="https://external-api.demo.kalshi.co/trade-api/v2",
    )
