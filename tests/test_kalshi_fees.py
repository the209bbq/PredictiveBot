from decimal import Decimal

from polymarket_bot.fees import kalshi_maker_fee, kalshi_taker_fee, round_up_cent


def test_kalshi_taker_table():
    assert kalshi_taker_fee(100, "0.50") == Decimal("1.75")
    assert kalshi_taker_fee(100, "0.10") == Decimal("0.63")
    assert kalshi_taker_fee(1, "0.50") == Decimal("0.02")
    assert kalshi_taker_fee(1, "0.01") == Decimal("0.01")


def test_kalshi_maker_free_by_default():
    assert kalshi_maker_fee(100, "0.50", fee_type="quadratic") == Decimal("0.00")


def test_kalshi_maker_fee_when_series_has_maker_fees():
    assert kalshi_maker_fee(100, "0.50", fee_type="quadratic_with_maker_fees") == Decimal("0.44")


def test_round_up_cent():
    assert round_up_cent(Decimal("0.0175")) == Decimal("0.02")
    assert round_up_cent(Decimal("1.75")) == Decimal("1.75")
