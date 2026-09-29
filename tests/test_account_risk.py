from decimal import Decimal

import pytest

from polymarket_bot.account_risk import (
    HARD_MAX_ACCOUNT_RISK_PCT,
    AccountRiskError,
    account_risk_fraction,
    total_at_risk,
    validate_account_risk_pct,
    worst_case_contract_risk,
)
from polymarket_bot.config import load_config
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.risk import snapshot_account_risk, would_breach_account_risk


def test_yes_buy_risk_is_price_times_qty():
    assert worst_case_contract_risk("buy", Decimal("0.40"), Decimal("10")) == Decimal("4.00")
    assert worst_case_contract_risk("yes", Decimal("0.40"), Decimal("10")) == Decimal("4.00")


def test_sell_no_risk_is_one_minus_price():
    assert worst_case_contract_risk("sell", Decimal("0.40"), Decimal("10")) == Decimal("6.00")
    assert worst_case_contract_risk("no", Decimal("0.25"), Decimal("4")) == Decimal("3.00")


def test_positions_plus_resting_orders_are_additive():
    positions = {"m": (Decimal("10"), Decimal("0.50"))}  # long 10 @ 0.50 → $5
    orders = [("buy", Decimal("0.40"), Decimal("10"))]  # + $4
    assert total_at_risk(positions, orders) == Decimal("9.00")


def test_risk_fraction_uses_account_value():
    assert account_risk_fraction(Decimal("400"), Decimal("1000")) == Decimal("0.4")
    assert account_risk_fraction(Decimal("1"), Decimal("0")) == Decimal("1")


def test_order_rejected_when_it_would_exceed_cap():
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    port.position("m").qty = Decimal("100")
    port.position("m").avg_price = Decimal("0.40")  # $40 at risk already
    proposed = PaperOrder("1", "m", "buy", Decimal("0.50"), Decimal("1000"), "maker")
    # extra $500 → 540/1000 = 54%
    assert (
        would_breach_account_risk(
            port, [], proposed, Decimal("1000"), Decimal("0.40")
        )
        == "account_risk_cap"
    )


def test_order_allowed_under_cap():
    port = Portfolio("maker", Decimal("1000"), Decimal("1000"))
    proposed = PaperOrder("1", "m", "buy", Decimal("0.50"), Decimal("10"), "maker")
    assert would_breach_account_risk(port, [], proposed, Decimal("1000"), Decimal("0.40")) is None
    at_risk, frac = snapshot_account_risk(port, [], Decimal("1000"), proposed)
    assert at_risk == Decimal("5.00")
    assert frac == Decimal("0.005")


def test_hard_max_refused_without_override():
    with pytest.raises(AccountRiskError, match="hard maximum"):
        validate_account_risk_pct(Decimal("0.50"), allow_above_hard_max=False)
    assert validate_account_risk_pct(HARD_MAX_ACCOUNT_RISK_PCT, allow_above_hard_max=False) == Decimal(
        "0.40"
    )
    assert validate_account_risk_pct(Decimal("0.50"), allow_above_hard_max=True) == Decimal("0.50")


def test_load_config_refuses_pct_above_hard_max(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text("dry_run: true\nlive_trading_enabled: false\npaper:\n  risk:\n    max_account_risk_pct: 0.50\n")
    with pytest.raises(AccountRiskError, match="hard maximum"):
        load_config(path)


def test_load_config_allows_override(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "dry_run: true\nlive_trading_enabled: false\n"
        "paper:\n  risk:\n    max_account_risk_pct: 0.50\n"
        "    allow_account_risk_above_hard_max: true\n"
    )
    cfg = load_config(path)
    assert cfg.paper.risk.max_account_risk_pct == Decimal("0.50")


def test_default_cap_is_hard_max():
    cfg = load_config()
    assert cfg.paper.risk.max_account_risk_pct == HARD_MAX_ACCOUNT_RISK_PCT
    assert cfg.paper.risk.allow_account_risk_above_hard_max is False
