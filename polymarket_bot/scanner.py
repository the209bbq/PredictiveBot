"""Read-only scanner over public Kalshi markets."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_bot.config import AppConfig, ScannerConfig
from polymarket_bot.market_data import MarketDataClient, MarketSnapshot
from polymarket_bot.market_data.errors import BookFetchError
from polymarket_bot.market_data.normalize import snapshot_from_payloads
from polymarket_bot.market_risk import attach_market_risk, format_score, market_over_risk_threshold
from polymarket_bot.series_filter import listed_market_ok, maker_universe_ok, touch_queue

log = logging.getLogger("polymarket_bot")
_LAST_SCAN_SUMMARY = ""
_SERIES_CACHE: dict[str, Any] = {"at": None, "rows": None, "ttl": 1800.0}
_LAST_SCAN_COVERAGE: dict[str, Any] = {}


def clear_scan_caches() -> None:
    """Drop cached GET /series rows (tests and a forced refresh)."""
    _SERIES_CACHE["at"] = None
    _SERIES_CACHE["rows"] = None
    _LAST_SCAN_COVERAGE.clear()


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


def last_scan_summary() -> str:
    return _LAST_SCAN_SUMMARY


def last_scan_coverage() -> dict[str, Any]:
    return dict(_LAST_SCAN_COVERAGE)


def _record_scan_summary(text: str) -> None:
    global _LAST_SCAN_SUMMARY
    _LAST_SCAN_SUMMARY = text
    log.info(text)


def _list_favorites_markets(client: MarketDataClient, config: AppConfig) -> list[dict]:
    """List KXHIGH*/KXRAIN* via /series + series_ticker, not the first 800 open markets."""
    from polymarket_bot.favorites import favorites_settings

    settings = favorites_settings(config)
    prefixes = [p.upper() for p in settings.allow if p]
    found: list[dict] = []
    seen: set[str] = set()

    def _add(rows: list[dict] | None) -> None:
        for market in rows or []:
            ticker = _market_ticker(market)
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            found.append(market)

    series_ids: list[str] = []
    catalog = _cached_series_catalog(client)
    for row in catalog:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("series_ticker") or "").upper()
        if ticker and any(ticker.startswith(p) for p in prefixes):
            series_ids.append(ticker)

    for prefix in prefixes:
        if prefix not in series_ids:
            series_ids.insert(0, prefix)
    # Exact allowlist first (KXRAIN, KXHIGH), then a bounded set of city series.
    exact = [p for p in prefixes]
    extra = [s for s in series_ids if s not in exact]
    series_ids = exact + extra[:24]
    log.info("favorites_scan series_queried=%s", series_ids)

    attempted = 0
    failed: list[str] = []
    first_series = True
    for series in series_ids:
        if not first_series:
            pacer = getattr(client, "pace_series_list", None)
            if callable(pacer):
                pacer()
        first_series = False
        attempted += 1
        try:
            batch = client.list_markets(
                limit=config.scanner.max_markets_to_list,
                active=config.scanner.active_only,
                closed=config.scanner.include_closed,
                series_ticker=series,
            )
        except TypeError:
            batch = None
        except Exception as exc:
            failed.append(series)
            log.info("favorites_scan series_markets_error series=%s err=%s", series, exc)
            continue
        if batch:
            _add(batch)

    coverage = {
        "series_attempted": attempted,
        "series_failed": len(failed),
        "series_ok": attempted - len(failed),
        "series_failed_ids": failed[:12],
        "series_queried": list(series_ids),
    }
    _LAST_SCAN_COVERAGE.update(coverage)
    log.info(
        "favorites_scan coverage series_attempted=%s series_failed=%s series_ok=%s failed_ids=%s",
        coverage["series_attempted"],
        coverage["series_failed"],
        coverage["series_ok"],
        coverage["series_failed_ids"],
    )

    if not found:
        try:
            _add(
                client.list_markets(
                    limit=config.scanner.max_markets_to_list,
                    active=config.scanner.active_only,
                    closed=config.scanner.include_closed,
                )
            )
        except Exception as exc:
            log.info("favorites_scan fallback_list_error=%s", exc)
    return found


def _cached_series_catalog(client: MarketDataClient) -> list[dict]:
    """Reuse GET /series for TTL seconds; market re-scans hit only needed series."""
    now = datetime.now(timezone.utc)
    cached_at = _SERIES_CACHE.get("at")
    cached_rows = _SERIES_CACHE.get("rows")
    ttl = float(_SERIES_CACHE.get("ttl") or 1800.0)
    if cached_at is not None and cached_rows is not None and (now - cached_at).total_seconds() < ttl:
        return list(cached_rows)
    rows: list[dict] = []
    lister = getattr(client, "list_series", None)
    if callable(lister):
        try:
            rows = [row for row in (lister() or []) if isinstance(row, dict)]
        except Exception as exc:
            log.info("favorites_scan series_list_error=%s", exc)
            if cached_rows:
                return list(cached_rows)
            return []
    _SERIES_CACHE["at"] = now
    _SERIES_CACHE["rows"] = rows
    return list(rows)


def _list_raw(client: MarketDataClient, config: AppConfig) -> list[dict]:
    from polymarket_bot.favorites import is_favorites_mode

    cfg = config.scanner
    if is_favorites_mode(config):
        return _list_favorites_markets(client, config)
    return client.list_markets(
        limit=cfg.max_markets_to_list,
        active=cfg.active_only,
        closed=cfg.include_closed,
        offset=0,
    )


def _market_ticker(market: dict) -> str:
    return str(market.get("ticker") or market.get("slug") or "")


def _rank_for_books(client, listed, now, config) -> list[dict]:
    from polymarket_bot.favorites import favorites_settings, is_favorites_mode

    cfg = config.scanner
    min_hours = cfg.min_hours_to_resolution
    max_hours = None
    if is_favorites_mode(config):
        settings = favorites_settings(config)
        min_hours = settings.min_hours_to_close
        max_hours = settings.max_hours_to_close
    ranked: list[tuple[Decimal, Decimal, str, dict]] = []
    favorites = is_favorites_mode(config)
    for market in listed:
        snap = _snapshot(client, market, None, now, config)
        hours = snap.hours_to_resolution
        if hours is not None and hours < min_hours:
            continue
        if max_hours is not None and hours is not None and hours > max_hours:
            continue
        if favorites:
            # Demo list rows often omit bid/ask. Rank by hours/volume; fetch the book next.
            spread = snap.spread if snap.spread is not None else Decimal("1")
            ranked.append((-liquidity_score(snap), spread, _market_ticker(market), market))
            continue
        if snap.best_bid is None or snap.best_ask is None or snap.spread is None:
            continue
        if snap.spread > cfg.max_spread:
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
    from polymarket_bot.favorites import book_skip_reason, is_favorites_mode, listed_skip_reason

    now = now or datetime.now(timezone.utc)
    raw = _list_raw(client, config)
    favorites = is_favorites_mode(config)
    reasons: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}

    def _count(reason: str, ticker: str) -> None:
        reasons[reason] += 1
        bucket = examples.setdefault(reason, [])
        if ticker and len(bucket) < 3:
            bucket.append(ticker)

    listed: list[dict] = []
    for market in raw:
        ticker = _market_ticker(market)
        if favorites:
            why = listed_skip_reason(market, config, now=now)
            if why:
                _count(why, ticker)
                continue
        elif not listed_market_ok(market, config, now=now):
            _count("series_filter", ticker)
            continue
        listed.append(market)

    picked = _rank_for_books(client, listed, now, config)
    not_ranked = len(listed) - len(picked)
    if not_ranked > 0:
        reasons["not_ranked_for_books"] += not_ranked
    snapshots = _fetch_books(client, picked, now, config)
    book_miss = len(picked) - len(snapshots)
    if book_miss > 0:
        reasons["book_fetch_failed"] += book_miss

    cap = config.paper.risk.max_market_risk_score
    kept = []
    for snap in snapshots:
        if favorites:
            why = book_skip_reason(snap, config, now=now, risk_cap=cap)
            if why:
                _count(why, snap.slug)
                continue
        else:
            if not is_liquid(snap, config.scanner):
                _count("illiquid", snap.slug)
                continue
            if not maker_universe_ok(snap, config, now=now):
                _count("series_filter", snap.slug)
                continue
            if market_over_risk_threshold(snap, cap):
                _count("market_risk_score", snap.slug)
                continue
        kept.append(snap)
    kept.sort(key=lambda s: (-liquidity_score(s), s.spread or Decimal("1"), s.slug or ""))
    kept = kept[: config.scanner.top_n]
    if favorites:
        empty_note = ""
        if not kept:
            empty_note = (
                " no eligible markets in the 15m–6h window on this host "
                "(demo may have none at this hour; pin --ticker if you have one)."
            )
        coverage = last_scan_coverage()
        _record_scan_summary(
            "favorites_scan listed=%s after_list=%s books=%s kept=%s "
            "series_attempted=%s series_failed=%s series_ok=%s "
            "by_reason=%s examples=%s%s"
            % (
                len(raw),
                len(listed),
                len(snapshots),
                len(kept),
                coverage.get("series_attempted", 0),
                coverage.get("series_failed", 0),
                coverage.get("series_ok", 0),
                dict(reasons),
                examples,
                empty_note,
            )
        )
    return kept


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
