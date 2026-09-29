"""Account-level worst-case risk: positions plus resting orders if they filled."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

HARD_MAX_ACCOUNT_RISK_PCT = Decimal("0.40")
ZERO = Decimal("0")
ONE = Decimal("1")


class AccountRiskError(ValueError):
    """Raised when account-risk config is invalid or an order would breach the cap."""


def validate_account_risk_pct(pct: Decimal, *, allow_above_hard_max: bool) -> Decimal:
    if pct < ZERO or pct > ONE:
        raise AccountRiskError("max_account_risk_pct must be between 0 and 1 inclusive.")
    if pct > HARD_MAX_ACCOUNT_RISK_PCT and not allow_above_hard_max:
        raise AccountRiskError(
            f"max_account_risk_pct {pct} exceeds the hard maximum "
            f"{HARD_MAX_ACCOUNT_RISK_PCT}. Set allow_account_risk_above_hard_max: true "
            "to override."
        )
    return pct


def worst_case_contract_risk(side: str, price: Decimal, qty: Decimal) -> Decimal:
    """Worst-case $ loss if this YES buy / sell (NO) fills.

    Buy/YES at p: lose p per contract if the contract expires at 0.
    Sell/NO at p: lose (1-p) per contract if the contract expires at 1.
    """
    qty = abs(Decimal(qty))
    price = Decimal(price)
    token = (side or "").lower()
    if token in {"buy", "bid", "yes", "long"}:
        return qty * price
    if token in {"sell", "ask", "no", "short"}:
        return qty * (ONE - price)
    raise ValueError(f"unknown side {side!r}")


def position_worst_case_risk(qty: Decimal, price: Decimal) -> Decimal:
    if qty > 0:
        return qty * price
    if qty < 0:
        return abs(qty) * (ONE - price)
    return ZERO


def total_at_risk(
    positions: dict[str, tuple[Decimal, Decimal]],
    orders: Iterable[tuple[str, Decimal, Decimal]],
) -> Decimal:
    """Worst-case $ loss: open positions plus every resting order if it filled."""
    risk = sum((position_worst_case_risk(qty, price) for qty, price in positions.values()), ZERO)
    risk += sum((worst_case_contract_risk(side, price, qty) for side, price, qty in orders), ZERO)
    return risk


def account_risk_fraction(at_risk: Decimal, account_value: Decimal) -> Decimal:
    if account_value <= ZERO:
        return ONE if at_risk > ZERO else ZERO
    return at_risk / account_value


def format_risk_pct(frac: Decimal) -> str:
    return f"{(frac * Decimal('100')).quantize(Decimal('0.01'))}%"
