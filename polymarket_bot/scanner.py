"""Read-only scanner over public Polymarket US markets."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.config import AppConfig, ScannerConfig
from polymarket_bot.market_data import MarketDataClient, MarketSnapshot
from polymarket_bot.market_data.normalize import snapshot_from_payloads


def is_liquid(snap: MarketSnapshot, cfg: ScannerConfig) -> bool:
    if snap.best_bid is None or snap.best_ask is None or snap.spread is None:
        return False
    if snap.spread > cfg.max_spread:
        return False
    if snap.bid_depth_contracts < cfg.min_bid_depth_contracts:
        return False
    if snap.ask_depth_contracts < cfg.min_ask_depth_contracts:
        return False
    volume = snap.volume_shares or Decimal("0")
    if volume < cfg.min_volume_shares:
        return False
    hours = snap.hours_to_resolution
    if hours is None or hours < cfg.min_hours_to_resolution:
        return False
    status = (snap.status or "").upper()
    if status and "OPEN" not in status:
        return False
    return True


def scan_markets(
    client: MarketDataClient,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> list[MarketSnapshot]:
    now = now or datetime.now(timezone.utc)
    cfg = config.scanner
    listed = client.list_markets(
        limit=cfg.max_markets_to_list,
        active=cfg.active_only,
        closed=cfg.include_closed,
        offset=0,
    )

    ranked: list[tuple[Decimal, dict]] = []
    for market in listed:
        snap = snapshot_from_payloads(
            market,
            now=now,
            tick_size_fallback=config.paper.tick_size_fallback,
        )
        if snap.best_bid is None or snap.best_ask is None or snap.spread is None:
            continue
        if snap.spread > cfg.max_spread:
            continue
        hours = snap.hours_to_resolution
        if hours is not None and hours < cfg.min_hours_to_resolution:
            continue
        ranked.append((snap.spread, market))

    ranked.sort(key=lambda row: row[0])
    snapshots: list[MarketSnapshot] = []
    for _, market in ranked[: cfg.max_book_fetches]:
        slug = market["slug"]
        try:
            book = client.book(slug)
        except Exception:
            continue
        snap = snapshot_from_payloads(
            market,
            book,
            None,
            now=now,
            tick_size_fallback=config.paper.tick_size_fallback,
        )
        snapshots.append(snap)

    liquid = [s for s in snapshots if is_liquid(s, cfg)]
    liquid.sort(key=lambda s: (s.spread or Decimal("1"), -(s.volume_shares or 0)))
    return liquid[: cfg.top_n]


def format_scan_table(rows: list[MarketSnapshot]) -> str:
    if not rows:
        return "No liquid markets matched the scanner filters."
    header = (
        f"{'slug':<42} {'mid':>7} {'sprd':>7} {'bidQty':>10} {'askQty':>10} "
        f"{'vol':>10} {'hrs':>8} question"
    )
    lines = [header, "-" * len(header)]
    for s in rows:
        mid = f"{s.mid:.3f}" if s.mid is not None else "-"
        sprd = f"{s.spread:.3f}" if s.spread is not None else "-"
        hrs = f"{s.hours_to_resolution:.1f}" if s.hours_to_resolution is not None else "-"
        vol = f"{s.volume_shares:.0f}" if s.volume_shares is not None else "-"
        q = (s.question or "")[:48]
        lines.append(
            f"{s.slug:<42} {mid:>7} {sprd:>7} {s.bid_depth_contracts:>10.0f} "
            f"{s.ask_depth_contracts:>10.0f} {vol:>10} {hrs:>8} {q}"
        )
    return "\n".join(lines)
