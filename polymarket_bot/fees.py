"""Polymarket US fee schedule effective 12:00 AM ET, Friday 25 Sep 2026.

Source: https://docs.polymarket.us/fees

Standard formula:
    Fee = Θ × C × p × (1 − p)

    Taker Θ = 0.0695 (a cost)
    Maker Θ = 0.0125 (a rebate credited at fill)

Combo taker formula:
    Fee = C × p × [0.0695 × (1 − p) + 0.04 × (1 − p)^4]

Maker rebates on combo fills still use the standard maker formula.

Fees/rebates are rounded to the nearest cent with banker's rounding
(round-half-to-even). A market may publish its own taker coefficient in
`feeCoefficient`; maker rebate theta is unchanged unless the docs change.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_UP, Decimal

CENTS = Decimal("0.01")
TAKER_THETA = Decimal("0.0695")
MAKER_REBATE_THETA = Decimal("0.0125")
COMBO_TAKER_EXTRA = Decimal("0.04")
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


def _as_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round_cents(amount: Decimal | int | float | str) -> Decimal:
    """Banker's rounding to the nearest cent (ROUND_HALF_EVEN)."""
    return _as_decimal(amount).quantize(CENTS, rounding=ROUND_HALF_EVEN)


def _validate(contracts: Decimal, price: Decimal) -> None:
    if contracts < 0:
        raise ValueError("contracts must be >= 0")
    if price < MIN_PRICE or price > MAX_PRICE:
        raise ValueError(f"price must be between {MIN_PRICE} and {MAX_PRICE}")


def uncertainty(price: Decimal | int | float | str) -> Decimal:
    p = _as_decimal(price)
    return p * (Decimal("1") - p)


def exact_taker_fee(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = TAKER_THETA,
    combo: bool = False,
) -> Decimal:
    c = _as_decimal(contracts)
    p = _as_decimal(price)
    _validate(c, p)
    one_minus_p = Decimal("1") - p
    t = _as_decimal(theta)
    if combo:
        return c * p * (t * one_minus_p + COMBO_TAKER_EXTRA * (one_minus_p**4))
    return t * c * p * one_minus_p


def exact_maker_rebate(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = MAKER_REBATE_THETA,
) -> Decimal:
    c = _as_decimal(contracts)
    p = _as_decimal(price)
    _validate(c, p)
    return _as_decimal(theta) * c * p * (Decimal("1") - p)


def taker_fee(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = TAKER_THETA,
    combo: bool = False,
) -> Decimal:
    """Taker cost as a positive Decimal in USD, rounded to cents."""
    return round_cents(exact_taker_fee(contracts, price, theta=theta, combo=combo))


def maker_rebate(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = MAKER_REBATE_THETA,
) -> Decimal:
    """Maker rebate as a positive Decimal in USD, rounded to cents."""
    return round_cents(exact_maker_rebate(contracts, price, theta=theta))


def signed_maker_cash(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
) -> Decimal:
    """Cash flow for a maker fill: +rebate."""
    return maker_rebate(contracts, price)


def signed_taker_cash(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    combo: bool = False,
    theta: Decimal | int | float | str = TAKER_THETA,
) -> Decimal:
    """Cash flow for a taker fill: −fee."""
    return -taker_fee(contracts, price, theta=theta, combo=combo)


# --- Kalshi (Trade API v2). Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf
# Effective schedule reviewed against docs.kalshi.com series fee_type (Jul 2026 PDF).
# Taker: round_up(M × 0.07 × C × P × (1−P)) to the next cent, per order.
# Maker: $0 unless the series fee_type includes maker fees, then
#        round_up(M × 0.0175 × C × P × (1−P)). Combo maker series use 0.035.

KALSHI_TAKER_THETA = Decimal("0.07")
KALSHI_MAKER_THETA = Decimal("0.0175")
KALSHI_COMBO_MAKER_THETA = Decimal("0.035")


def round_up_cent(amount: Decimal | int | float | str) -> Decimal:
    value = _as_decimal(amount)
    if value <= 0:
        return Decimal("0.00")
    return (value * 100).to_integral_value(rounding=ROUND_UP) / 100


def kalshi_taker_fee(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    multiplier: Decimal | int | float | str = 1,
) -> Decimal:
    c = _as_decimal(contracts)
    p = _as_decimal(price)
    _validate(c, p)
    raw = _as_decimal(multiplier) * KALSHI_TAKER_THETA * c * p * (Decimal("1") - p)
    return round_up_cent(raw)


def kalshi_maker_fee(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    fee_type: str | None = None,
    multiplier: Decimal | int | float | str = 1,
) -> Decimal:
    kind = (fee_type or "quadratic").lower()
    if "maker_fees" not in kind:
        return Decimal("0.00")
    c = _as_decimal(contracts)
    p = _as_decimal(price)
    _validate(c, p)
    theta = KALSHI_COMBO_MAKER_THETA if "combo" in kind else KALSHI_MAKER_THETA
    raw = _as_decimal(multiplier) * theta * c * p * (Decimal("1") - p)
    return round_up_cent(raw)


def venue_taker_fee(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    venue: str = "polymarket_us",
    fee_type: str | None = None,
    multiplier: Decimal | int | float | str = 1,
    theta: Decimal | int | float | str | None = None,
) -> Decimal:
    if venue == "kalshi":
        return kalshi_taker_fee(contracts, price, multiplier=multiplier)
    return taker_fee(contracts, price, theta=theta or TAKER_THETA, combo=False)


def maker_cashflow(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    venue: str = "polymarket_us",
    fee_type: str | None = None,
    multiplier: Decimal | int | float | str = 1,
) -> Decimal:
    """Maker cash at fill: +rebate on Polymarket US, −fee (often 0) on Kalshi."""
    if venue == "kalshi":
        return -kalshi_maker_fee(contracts, price, fee_type=fee_type, multiplier=multiplier)
    return maker_rebate(contracts, price)
