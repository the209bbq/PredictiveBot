"""Risk controls shared by both paper strategies."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from polymarket_bot.account_risk import (
    HARD_MAX_ACCOUNT_RISK_PCT,
    AccountRiskError,
    account_risk_fraction,
    format_risk_pct,
    total_at_risk,
    validate_account_risk_pct,
    worst_case_contract_risk,
)
from polymarket_bot.config import RiskConfig
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio

ZERO = Decimal("0")

__all__ = [
    "HARD_MAX_ACCOUNT_RISK_PCT",
    "AccountRiskError",
    "account_risk_fraction",
    "account_value_from_portfolio",
    "check_daily_loss",
    "format_risk_pct",
    "past_resolution_cutoff",
    "positions_from_portfolio",
    "price_jumped",
    "snapshot_account_risk",
    "total_at_risk",
    "validate_account_risk_pct",
    "would_breach_account_risk",
    "would_breach_risk_limits",
    "would_breach_position",
    "worst_case_contract_risk",
]


def price_jumped(prev: MarketSnapshot | None, cur: MarketSnapshot, threshold: Decimal) -> bool:
    if prev is None or prev.mid is None or cur.mid is None:
        return False
    return abs(cur.mid - prev.mid) >= threshold


def past_resolution_cutoff(cur: MarketSnapshot, min_hours: float) -> bool:
    hours = cur.hours_to_resolution
    return hours is None or hours < min_hours


def would_breach_position(
    portfolio: Portfolio,
    market: str,
    side: str,
    qty: Decimal,
    risk: RiskConfig,
) -> str | None:
    pos = portfolio.position(market).qty
    signed = qty if side == "buy" else -qty
    new_pos = pos + signed
    if abs(new_pos) > risk.max_position_per_market:
        return "per_market_cap"
    new_gross = portfolio.gross_position() - abs(pos) + abs(new_pos)
    if new_gross > risk.max_gross_position:
        return "gross_cap"
    return None


def check_daily_loss(
    portfolio: Portfolio,
    mids: dict,
    risk: RiskConfig,
    *,
    start_equity: Decimal | None = None,
) -> str | None:
    eq = portfolio.equity(mids)
    start = start_equity if start_equity is not None else portfolio.starting_cash
    limit = getattr(risk, "max_daily_loss_usd", None) or risk.max_daily_loss
    if eq - start <= -limit:
        return "daily_loss_limit"
    return None


def positions_from_portfolio(portfolio: Portfolio) -> dict[str, tuple[Decimal, Decimal]]:
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for slug, pos in portfolio.positions.items():
        if pos.qty == 0:
            continue
        out[slug] = (pos.qty, pos.avg_price or ZERO)
    return out


def snapshot_account_risk(
    portfolio: Portfolio,
    resting: Iterable[PaperOrder],
    account_value: Decimal,
    proposed: PaperOrder | None = None,
) -> tuple[Decimal, Decimal]:
    orders = [(o.side, o.price, o.qty) for o in resting]
    if proposed is not None:
        orders.append((proposed.side, proposed.price, proposed.qty))
    at_risk = total_at_risk(positions_from_portfolio(portfolio), orders)
    return at_risk, account_risk_fraction(at_risk, account_value)


def would_breach_account_risk(
    portfolio: Portfolio,
    resting: Iterable[PaperOrder],
    proposed: PaperOrder,
    account_value: Decimal,
    cap: Decimal,
    capital_cap: Decimal | None = None,
) -> str | None:
    at_risk, frac = snapshot_account_risk(portfolio, resting, account_value, proposed)
    reasons = []
    if frac > cap:
        reasons.append("account_risk_cap")
    if capital_cap is not None and at_risk > capital_cap:
        reasons.append("daily_capital_cap")
    return reasons[0] if reasons else None


def would_breach_risk_limits(
    portfolio: Portfolio,
    resting: Iterable[PaperOrder],
    proposed: PaperOrder,
    account_value: Decimal,
    risk: RiskConfig,
) -> str | None:
    return would_breach_account_risk(
        portfolio,
        resting,
        proposed,
        account_value,
        risk.max_account_risk_pct,
        capital_cap=risk.max_daily_capital_in_use_usd,
    )


def account_value_from_portfolio(portfolio: Portfolio, mids: dict[str, Decimal | None]) -> Decimal:
    merged = dict(mids)
    for slug, pos in portfolio.positions.items():
        if slug not in merged or merged[slug] is None:
            merged[slug] = pos.avg_price or None
    return portfolio.equity(merged)
