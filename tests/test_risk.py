from decimal import Decimal

from polymarket_bot.config import load_config
from polymarket_bot.paper.portfolio import Portfolio
from polymarket_bot.paper.risk import check_daily_loss, would_breach_position


def test_per_market_and_gross_caps():
    cfg = load_config().paper.risk
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    port.position("a").qty = Decimal("40")
    assert would_breach_position(port, "a", "buy", Decimal("10"), cfg) == "per_market_cap"
    port.position("a").qty = Decimal("10")
    port.position("b").qty = Decimal("110")
    assert would_breach_position(port, "a", "buy", Decimal("10"), cfg) == "gross_cap"
    port.position("b").qty = Decimal("100")
    assert would_breach_position(port, "c", "buy", Decimal("5"), cfg) is None


def test_daily_loss_kill_switch():
    cfg = load_config().paper.risk
    port = Portfolio("maker", Decimal("940"), Decimal("1000"))
    mids = {"a": Decimal("0.50")}
    port.position("a").qty = Decimal("0")
    assert check_daily_loss(port, mids, cfg) == "daily_loss_limit"
    port.cash = Decimal("990")
    assert check_daily_loss(port, mids, cfg) is None
