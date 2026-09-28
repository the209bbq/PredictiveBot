"""Risk controls shared by both paper strategies."""

from __future__ import annotations

from decimal import Decimal

from polymarket_bot.config import RiskConfig
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.paper.portfolio import Portfolio


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


def check_daily_loss(portfolio: Portfolio, mids: dict, risk: RiskConfig) -> str | None:
    eq = portfolio.equity(mids)
    loss = portfolio.starting_cash - eq
    if loss >= risk.max_daily_loss:
        return "max_daily_loss"
    return None
