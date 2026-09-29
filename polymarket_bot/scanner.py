"""Read-only scanner over public Kalshi markets."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.config import AppConfig, ScannerConfig
from polymarket_bot.market_data import MarketDataClient, MarketSnapshot
from polymarket_bot.market_data.errors import BookFetchError
from polymarket_bot.market_data.normalize import snapshot_from_payloads
from polymarket_bot.market_risk import attach_market_risk, format_score, market_over_risk_threshold
from polymarket_bot.series_filter import listed_market_ok, maker_universe_ok, touch_queue


def _snapshot(client: MarketDataClient, market: dict, book, now, config: AppConfig) -> MarketSnapshot:
    if hasattr(client, "snapshot"):
        return client.snapshot(market, book, now=now)
    return snapshot_from_payloads(
        market,
        book,
        None,
        now=now,
        tick_size_fallback=config.paper.tick_size_fallback,
    )


def liquidity_score(snap: MarketSnapshot) -> Decimal:
    volume = snap.volume_shares or Decimal("0")
    oi = snap.open_interest or Decimal("0")
    liq = snap.notional_traded or Decimal("0")
    depth = (snap.bid_depth_contracts or Decimal("0")) + (snap.ask_depth_contracts or Decimal("0"))
    return volume * Decimal("10") + oi + liq + depth + touch_queue(snap) * Decimal("5")


def is_liquid(snap: MarketSnapshot, cfg: ScannerConfig) -> bool:
    if snap.stale or not snap.book_fetched:
        return False
    if snap.best_bid is None or snap.best_ask is None or snap.spread is None:
        return False
    if snap.spread > cfg.max_spread:
        return False
    if snap.bid_depth_contracts < cfg.min_bid_depth_contracts:
        return False
    if snap.ask_depth_contracts < cfg.min_ask_depth_contracts:
        return False
    volume = snap.volume_shares or Decimal("0")
    # Kalshi list volume is often 0; accept depth/OI as a substitute.
    if volume < cfg.min_volume_shares and liquidity_score(snap) <= 0:
        return False
    hours = snap.hours_to_resolution
    if hours is None or hours < cfg.min_hours_to_resolution:
        return False
    status = (snap.status or "").lower()
    if status and not any(tok in status for tok in ("open", "active")):
        return False
    return True


def _list_raw(client: MarketDataClient, config: AppConfig) -> list[dict]:
    cfg = config.scanner
    return client.list_markets(
        limit=cfg.max_markets_to_list,
        active=cfg.active_only,
        closed=cfg.include_closed,
        offset=0,
    )


def _market_ticker(market: dict) -> str:
    return str(market.get("ticker") or market.get("slug") or "")


def _rank_for_books(client, listed, now, config) -> list[dict]:
    cfg = config.scanner
    ranked: list[tuple[Decimal, Decimal, str, dict]] = []
    for market in listed:
        snap = _snapshot(client, market, None, now, config)
        if snap.best_bid is None or snap.best_ask is None or snap.spread is None:
            continue
        if snap.spread > cfg.max_spread:
            continue
        hours = snap.hours_to_resolution
        if hours is not None and hours < cfg.min_hours_to_resolution:
            continue
        ranked.append((-liquidity_score(snap), snap.spread, _market_ticker(market), market))
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))
    return [market for _, _, _, market in ranked[: cfg.max_book_fetches]]


def _fetch_books(client, markets, now, config) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    for market in markets:
        slug = market.get("slug") or market.get("ticker")
        try:
            book = client.book(slug)
        except (BookFetchError, Exception):
            continue
        snap = _snapshot(client, market, book, now, config)
        snap.stale = False
        snap.book_fetched = True
        attach_market_risk(snap)
        snapshots.append(snap)
    return snapshots


def scan_markets(
    client: MarketDataClient,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> list[MarketSnapshot]:
    now = now or datetime.now(timezone.utc)
    listed = _list_raw(client, config)
    picked = _rank_for_books(client, listed, now, config)
    snapshots = _fetch_books(client, picked, now, config)
    liquid = [s for s in snapshots if is_liquid(s, config.scanner)]
    liquid.sort(key=lambda s: (-liquidity_score(s), s.spread or Decimal("1"), s.slug or ""))
    return liquid[: config.scanner.top_n]


def scan_maker_universe(
    client: MarketDataClient,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> list[MarketSnapshot]:
    """Allowlisted maker markets only. Filters series before spending book fetches."""
    now = now or datetime.now(timezone.utc)
    listed = [m for m in _list_raw(client, config) if listed_market_ok(m, config, now=now)]
    picked = _rank_for_books(client, listed, now, config)
    snapshots = _fetch_books(client, picked, now, config)
    cap = config.paper.risk.max_market_risk_score
    kept = []
    for snap in snapshots:
        if not is_liquid(snap, config.scanner):
            continue
        if not maker_universe_ok(snap, config, now=now):
            continue
        if market_over_risk_threshold(snap, cap):
            continue
        kept.append(snap)
    kept.sort(key=lambda s: (-liquidity_score(s), s.spread or Decimal("1"), s.slug or ""))
    return kept[: config.scanner.top_n]


def scan_near_resolution(
    client: MarketDataClient,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> list[MarketSnapshot]:
    """Own universe: close-to-event favorites, not the far-dated maker book."""
    now = now or datetime.now(timezone.utc)
    near = config.paper.near_resolution
    if not near.enabled:
        return []
    listed = _list_raw(client, config)
    candidates: list[tuple[Decimal, dict]] = []
    for market in listed:
        snap = _snapshot(client, market, None, now, config)
        hours = snap.hours_to_resolution
        if hours is None:
            continue
        if hours > near.max_hours_to_resolution or hours < near.min_hours_to_resolution:
            continue
        if snap.mid is None or snap.mid < near.min_price or snap.mid > near.max_price:
            continue
        if snap.spread is not None and snap.spread > config.scanner.max_spread:
            continue
        candidates.append((-liquidity_score(snap), _market_ticker(market), market))
    candidates.sort(key=lambda row: (row[0], row[1]))
    picked = [market for _, _, market in candidates[: config.scanner.max_book_fetches]]
    snapshots = _fetch_books(client, picked, now, config)
    kept = []
    for snap in snapshots:
        if snap.stale or not snap.book_fetched:
            continue
        hours = snap.hours_to_resolution
        if hours is None:
            continue
        if hours > near.max_hours_to_resolution or hours < near.min_hours_to_resolution:
            continue
        if snap.mid is None or snap.mid < near.min_price or snap.mid > near.max_price:
            continue
        kept.append(snap)
    kept.sort(key=lambda s: (s.hours_to_resolution or 99, -(s.mid or 0)))
    return kept[: config.paper.max_markets]


def format_scan_table(rows: list[MarketSnapshot]) -> str:
    if not rows:
        return "No liquid markets matched the scanner filters."
    header = (
        f"{'venue':<14} {'slug':<42} {'mid':>7} {'sprd':>7} {'risk':>6} {'bidQty':>10} "
        f"{'askQty':>10} {'vol':>10} {'hrs':>8} question"
    )
    lines = [header, "-" * len(header)]
    for s in rows:
        mid = f"{s.mid:.3f}" if s.mid is not None else "-"
        sprd = f"{s.spread:.3f}" if s.spread is not None else "-"
        hrs = f"{s.hours_to_resolution:.1f}" if s.hours_to_resolution is not None else "-"
        vol = f"{s.volume_shares:.0f}" if s.volume_shares is not None else "-"
        q = (s.question or "")[:48]
        stale = " [STALE]" if s.stale else ""
        risk = format_score(s.risk_score)
        lines.append(
            f"{s.venue:<14} {s.slug:<42} {mid:>7} {sprd:>7} {risk:>6} {s.bid_depth_contracts:>10.0f} "
            f"{s.ask_depth_contracts:>10.0f} {vol:>10} {hrs:>8} {q}{stale}"
        )
    return "\n".join(lines)
