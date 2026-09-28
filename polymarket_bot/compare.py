"""Read-only Kalshi vs Polymarket US comparison. Alerts only — never trades."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from polymarket_bot.config import AppConfig
from polymarket_bot.fees import venue_taker_fee
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.scanner import _snapshot

STOP = {
    "the", "a", "an", "of", "on", "in", "at", "for", "to", "will", "be", "by", "vs",
    "versus", "and", "or", "is", "over", "under", "more", "than", "game", "wins",
    "win", "before", "after", "yes", "no", "market", "contract", "price",
}


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in STOP and len(w) > 1}


def title_score(left: str, right: str) -> float:
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class VenueGap:
    kalshi: MarketSnapshot
    polymarket: MarketSnapshot
    score: float
    buy_kalshi_sell_pm: Decimal
    buy_pm_sell_kalshi: Decimal
    kalshi_taker: Decimal
    pm_taker_buy: Decimal
    pm_taker_sell: Decimal


def compare_snapshots(
    kalshi_rows: Iterable[MarketSnapshot],
    pm_rows: Iterable[MarketSnapshot],
    config: AppConfig,
) -> list[VenueGap]:
    size = config.compare.contract_size
    out: list[VenueGap] = []
    pm_list = list(pm_rows)
    used: set[str] = set()
    for k in kalshi_rows:
        k_title = " ".join(filter(None, [k.event_title, k.question, k.slug]))
        best: tuple[float, MarketSnapshot] | None = None
        for p in pm_list:
            if p.slug in used:
                continue
            p_title = " ".join(filter(None, [p.event_title, p.question, p.slug]))
            score = title_score(k_title, p_title)
            if score < config.compare.min_title_score:
                continue
            if best is None or score > best[0]:
                best = (score, p)
        if best is None:
            continue
        score, p = best
        if k.best_ask is None or k.best_bid is None or p.best_ask is None or p.best_bid is None:
            continue
        k_taker_buy = venue_taker_fee(size, k.best_ask, venue="kalshi", fee_type=k.fee_type, multiplier=k.fee_multiplier)
        k_taker_sell = venue_taker_fee(size, k.best_bid, venue="kalshi", fee_type=k.fee_type, multiplier=k.fee_multiplier)
        pm_taker_buy = venue_taker_fee(size, p.best_ask, venue="polymarket_us", theta=p.fee_coefficient)
        pm_taker_sell = venue_taker_fee(size, p.best_bid, venue="polymarket_us", theta=p.fee_coefficient)
        # Net $ for buying `size` contracts on A (at ask) and selling on B (at bid)
        buy_k_sell_p = (p.best_bid - k.best_ask) * size - k_taker_buy - pm_taker_sell
        buy_p_sell_k = (k.best_bid - p.best_ask) * size - pm_taker_buy - k_taker_sell
        out.append(
            VenueGap(
                kalshi=k,
                polymarket=p,
                score=score,
                buy_kalshi_sell_pm=buy_k_sell_p,
                buy_pm_sell_kalshi=buy_p_sell_k,
                kalshi_taker=k_taker_buy,
                pm_taker_buy=pm_taker_buy,
                pm_taker_sell=pm_taker_sell,
            )
        )
        used.add(p.slug)
    out.sort(key=lambda g: max(g.buy_kalshi_sell_pm, g.buy_pm_sell_kalshi), reverse=True)
    return out


def collect_snapshots(client, config: AppConfig, *, now: datetime | None = None) -> list[MarketSnapshot]:
    now = now or datetime.now(timezone.utc)
    listed = client.list_markets(limit=config.compare.max_markets_each, active=True, closed=False)
    rows: list[MarketSnapshot] = []
    for market in listed[: config.compare.max_markets_each]:
        slug = market.get("slug") or market.get("ticker")
        try:
            book = client.book(slug)
        except Exception:
            book = None
        snap = _snapshot(client, market, book, now, config)
        if snap.best_bid is None or snap.best_ask is None:
            continue
        rows.append(snap)
    return rows


def format_compare_report(gaps: list[VenueGap], config: AppConfig) -> str:
    lines = [
        "Cross-venue comparison (Kalshi vs Polymarket US)",
        "READ-ONLY — alerts only, no orders are sent on either venue",
        f"Taker fees applied on {config.compare.contract_size} contracts per side.",
        "",
    ]
    if not gaps:
        lines.append("No title matches above the similarity cutoff.")
        return "\n".join(lines) + "\n"
    alerts = 0
    for gap in gaps:
        edge = max(gap.buy_kalshi_sell_pm, gap.buy_pm_sell_kalshi)
        flag = " ALERT" if edge >= config.compare.min_net_edge else ""
        if flag:
            alerts += 1
        direction = (
            "buy Kalshi / sell Polymarket"
            if gap.buy_kalshi_sell_pm >= gap.buy_pm_sell_kalshi
            else "buy Polymarket / sell Kalshi"
        )
        lines.append(
            f"[{gap.score:.2f}]{flag} {gap.kalshi.slug}  vs  {gap.polymarket.slug}"
        )
        lines.append(f"  Kalshi: {gap.kalshi.question[:80]}")
        lines.append(f"  Polymarket: {gap.polymarket.question[:80]}")
        lines.append(
            f"  K {gap.kalshi.best_bid}/{gap.kalshi.best_ask}   "
            f"PM {gap.polymarket.best_bid}/{gap.polymarket.best_ask}"
        )
        lines.append(
            f"  Net after taker fees: buyK-sellPM {gap.buy_kalshi_sell_pm:.2f}  "
            f"buyPM-sellK {gap.buy_pm_sell_kalshi:.2f}  ({direction})"
        )
        lines.append("")
    lines.append(f"{len(gaps)} matched markets, {alerts} above alert threshold {config.compare.min_net_edge}.")
    lines.append("These gaps are not executable here (non-atomic, different rules, demo vs live books).")
    return "\n".join(lines) + "\n"
