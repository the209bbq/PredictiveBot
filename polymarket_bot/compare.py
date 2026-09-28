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
    "win", "before", "after", "yes", "no", "market", "contract", "price", "pro",
    "tec", "26", "09", "27",
}

ALIASES = {
    "mlb": {"baseball"},
    "baseball": {"mlb"},
    "champ": {"champion", "champions", "championship"},
    "champion": {"champ", "champions", "championship"},
    "champions": {"champ", "champion", "championship"},
    "nlchamp": {"national", "league", "champion"},
    "alchamp": {"american", "league", "champion"},
    "atl": {"atlanta"},
    "atlanta": {"atl"},
    "phi": {"philadelphia"},
    "philadelphia": {"phi"},
    "sd": {"diego"},
    "diego": {"sd"},
    "chc": {"chicago", "cubs"},
    "cws": {"chicago"},
    "nyy": {"york"},
    "lad": {"angeles"},
    "angeles": {"lad"},
    "bos": {"boston"},
    "boston": {"bos"},
    "hou": {"houston"},
    "houston": {"hou"},
    "mil": {"milwaukee"},
    "milwaukee": {"mil"},
}

KALSHI_COMPARE_SERIES = ("KXMLB", "KXMLBNL", "KXMLBAL", "KXMLBSERIES")


def team_code(slug: str) -> str:
    parts = re.findall(r"[a-z0-9]+", (slug or "").lower())
    return parts[-1] if parts else ""


def event_kind(slug: str, title: str) -> str:
    blob = f"{slug} {title}".lower()
    if "kxmlbnl" in blob or "nlchamp" in blob or "national league" in blob:
        return "nl"
    if "kxmlbal" in blob or "alchamp" in blob or "american league" in blob:
        return "al"
    if "world series" in blob or "mlb-champ" in blob:
        return "ws"
    if re.match(r"kxmlb-\d+-", (slug or "").lower()):
        return "ws"
    return ""


def same_team(left_slug: str, right_slug: str) -> bool | None:
    a, b = team_code(left_slug), team_code(right_slug)
    if not a or not b:
        return None
    if a == b:
        return True
    return b in ALIASES.get(a, set()) or a in ALIASES.get(b, set())


def tokens(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    expanded = set(words)
    for word in words:
        expanded |= ALIASES.get(word, set())
    return {w for w in expanded if w not in STOP and len(w) > 1}


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
        k_kind = event_kind(k.slug, k_title)
        best: tuple[float, MarketSnapshot] | None = None
        for p in pm_list:
            if p.slug in used:
                continue
            p_title = " ".join(filter(None, [p.event_title, p.question, p.slug]))
            p_kind = event_kind(p.slug, p_title)
            if k_kind and p_kind and k_kind != p_kind:
                continue
            team_ok = same_team(k.slug, p.slug)
            if team_ok is False:
                continue
            score = title_score(k_title, p_title)
            if team_ok:
                score = max(score, config.compare.min_title_score) + 0.1
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
    listed: list[dict] = []
    seen: set[str] = set()

    def _add(rows: list[dict]) -> None:
        for market in rows:
            key = str(market.get("ticker") or market.get("slug") or "")
            if not key or key in seen:
                continue
            listed.append(market)
            seen.add(key)

    if getattr(client, "venue", "") == "kalshi":
        for series in KALSHI_COMPARE_SERIES:
            try:
                _add(
                    client.list_markets(
                        limit=30,
                        active=True,
                        closed=False,
                        series_ticker=series,
                    )
                )
            except TypeError:
                break
    _add(client.list_markets(limit=config.compare.max_markets_each, active=True, closed=False))
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
