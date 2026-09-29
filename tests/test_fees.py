from decimal import Decimal

import pytest

from polymarket_bot.fees import (
    exact_taker_fee,
    maker_rebate,
    round_cents,
    taker_fee,
)


@pytest.mark.parametrize(
    ("contracts", "price", "taker", "maker"),
    [
        (1000, "0.10", "6.26", "1.12"),
        (1000, "0.65", "15.81", "2.84"),
        (1000, "0.30", "14.60", "2.62"),
        (1000, "0.90", "6.26", "1.12"),
        (1000, "0.50", "17.38", "3.12"),
    ],
)
def test_docs_worked_examples(contracts, price, taker, maker):
    assert taker_fee(contracts, price) == Decimal(taker)
    assert maker_rebate(contracts, price) == Decimal(maker)


@pytest.mark.parametrize(
    ("price", "taker", "maker"),
    [
        ("0.01", "0.07", "0.01"),
        ("0.10", "0.63", "0.11"),
        ("0.25", "1.30", "0.23"),
        ("0.43", "1.70", "0.31"),
        ("0.50", "1.74", "0.31"),
        ("0.75", "1.30", "0.23"),
        ("0.99", "0.07", "0.01"),
    ],
)
def test_standard_fee_table_100_lot(price, taker, maker):
    assert taker_fee(100, price) == Decimal(taker)
    assert maker_rebate(100, price) == Decimal(maker)


def test_combo_taker_examples():
    assert taker_fee(1000, "0.10", combo=True) == Decimal("8.88")
    # 18.625 banker's-rounds to 18.62
    assert exact_taker_fee(1000, "0.50", combo=True) == Decimal("18.625")
    assert taker_fee(1000, "0.50", combo=True) == Decimal("18.62")


def test_bankers_rounding_examples_from_docs():
    assert round_cents(Decimal("0.025")) == Decimal("0.02")
    assert round_cents(Decimal("0.035")) == Decimal("0.04")


def test_tiny_trade_can_round_to_zero():
    assert taker_fee(1, "0.01") == Decimal("0.00")


def test_rejects_out_of_range_price():
    with pytest.raises(ValueError):
        taker_fee(10, "0")
    with pytest.raises(ValueError):
        maker_rebate(10, "1.00")
