"""Paper-trading loop. Simulated quotes only; no live orders."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_bot.config import AppConfig
from polymarket_bot.logging_utils import DecisionLogger
from polymarket_bot.market_data import MarketDataClient, MarketSnapshot
from polymarket_bot.market_data.normalize import snapshot_from_payloads
from polymarket_bot.market_data.replay_client import ReplayClient
from polymarket_bot.paper import maker, near_resolution
from polymarket_bot.paper.fills import fill_reason
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.risk import check_daily_loss, past_resolution_cutoff, price_jumped
from polymarket_bot.scanner import scan_markets


def _mids(snaps: dict[str, MarketSnapshot]) -> dict[str, Decimal | None]:
    return {slug: snap.mid for slug, snap in snaps.items()}


def _cancel_all(orders: list[PaperOrder], logger: DecisionLogger, reason: str, strategy: str) -> list[PaperOrder]:
    for order in orders:
        logger.log("cancel", strategy=strategy, market=order.market, side=order.side, reason=reason, live=False)
    return []


def run_paper(
    client: MarketDataClient,
    config: AppConfig,
    logger: DecisionLogger,
    *,
    ticks: int | None = None,
    sleep: bool = True,
    now: datetime | None = None,
    markets: list[MarketSnapshot] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    n_ticks = ticks if ticks is not None else config.paper.ticks
    maker_book: list[PaperOrder] = []
    near_book: list[PaperOrder] = []
    maker_port = Portfolio("maker", config.paper.starting_cash, config.paper.starting_cash)
    near_port = Portfolio("near_resolution", config.paper.starting_cash, config.paper.starting_cash)
    prev: dict[str, MarketSnapshot] = {}

    if markets is None:
        markets = scan_markets(client, config, now=now)
    chosen = markets[: config.paper.max_markets]
    logger.log(
        "paper_start",
        source=client.source_name,
        live=False,
        dry_run=True,
        markets=[m.slug for m in chosen],
        ticks=n_ticks,
    )

    replay = client if isinstance(client, ReplayClient) else None

    for tick in range(n_ticks):
        snaps: dict[str, MarketSnapshot] = {}
        universe = chosen
        if replay is not None:
            listed = replay.list_markets(limit=config.scanner.max_markets_to_list, active=True, closed=False)
            universe = []
            for market in listed:
                slug = market["slug"]
                snap = snapshot_from_payloads(
                    market,
                    replay.book(slug),
                    None,
                    now=now,
                    tick_size_fallback=config.paper.tick_size_fallback,
                )
                universe.append(snap)
        else:
            refreshed: list[MarketSnapshot] = []
            for snap in chosen:
                try:
                    book = client.book(snap.slug)
                    refreshed.append(
                        snapshot_from_payloads(
                            snap.raw.get("market") or {"slug": snap.slug, "question": snap.question,
                                                       "endDate": snap.end_date.isoformat() if snap.end_date else None,
                                                       "category": snap.category, "status": snap.status,
                                                       "orderPriceMinTickSize": snap.tick_size,
                                                       "feeCoefficient": snap.fee_coefficient},
                            book,
                            None,
                            now=datetime.now(timezone.utc),
                            tick_size_fallback=config.paper.tick_size_fallback,
                        )
                    )
                except Exception as exc:
                    logger.log("book_error", market=snap.slug, error=str(exc), live=False)
                    refreshed.append(snap)
            universe = refreshed
            chosen = refreshed

        for snap in universe:
            snaps[snap.slug] = snap

        mids = _mids(snaps)
        for port in (maker_port, near_port):
            port.record_equity(mids)
            reason = check_daily_loss(port, mids, config.paper.risk)
            if reason and not port.killed:
                port.killed = True
                port.kill_reason = reason
                logger.log("kill_switch", strategy=port.name, reason=reason, live=False)

        if maker_port.killed:
            maker_book = _cancel_all(maker_book, logger, maker_port.kill_reason or "killed", "maker")
        if near_port.killed:
            near_book = _cancel_all(near_book, logger, near_port.kill_reason or "killed", "near_resolution")

        def process_book(book: list[PaperOrder], port: Portfolio) -> list[PaperOrder]:
            remaining: list[PaperOrder] = []
            if port.killed:
                return remaining
            for order in book:
                snap = snaps.get(order.market)
                if snap is None:
                    remaining.append(order)
                    continue
                if price_jumped(prev.get(order.market), snap, config.paper.risk.cancel_on_price_jump):
                    logger.log(
                        "cancel",
                        strategy=order.strategy,
                        market=order.market,
                        reason="price_jump",
                        live=False,
                    )
                    continue
                min_hours = (
                    config.paper.risk.maker_min_hours_to_resolution
                    if order.strategy == "maker"
                    else config.paper.near_resolution.min_hours_to_resolution
                )
                if past_resolution_cutoff(snap, min_hours):
                    logger.log(
                        "cancel",
                        strategy=order.strategy,
                        market=order.market,
                        reason="resolution_cutoff",
                        live=False,
                    )
                    continue
                reason = fill_reason(
                    order,
                    snap,
                    prev.get(order.market),
                    strict=config.paper.fills.require_strict_trade_through,
                )
                if reason:
                    fill = port.apply_fill(order, order.qty, reason)
                    logger.log(
                        "fill",
                        strategy=order.strategy,
                        market=order.market,
                        side=order.side,
                        price=order.price,
                        qty=order.qty,
                        rebate=fill.rebate,
                        reason=reason,
                        live=False,
                    )
                    continue
                remaining.append(order)
            return remaining

        maker_book = process_book(maker_book, maker_port)
        near_book = process_book(near_book, near_port)

        # Replace quotes each tick (simulated cancel/replace, never sent live).
        maker_book = _cancel_all(maker_book, logger, "requote", "maker") if maker_book else []
        near_book = _cancel_all(near_book, logger, "requote", "near_resolution") if near_book else []

        prefix = f"t{tick}"
        if not maker_port.killed:
            for snap in universe:
                quotes = maker.desired_quotes(snap, maker_port, config, prefix)
                for order in quotes:
                    maker_book.append(order)
                    logger.log(
                        "quote",
                        strategy="maker",
                        market=order.market,
                        side=order.side,
                        price=order.price,
                        qty=order.qty,
                        live=False,
                    )
        if not near_port.killed:
            for snap in universe:
                quotes = near_resolution.desired_quotes(snap, near_port, config, prefix)
                for order in quotes:
                    near_book.append(order)
                    logger.log(
                        "quote",
                        strategy="near_resolution",
                        market=order.market,
                        side=order.side,
                        price=order.price,
                        qty=order.qty,
                        live=False,
                    )

        prev = snaps
        if replay is not None:
            replay.advance()
        elif sleep and tick < n_ticks - 1:
            time.sleep(config.paper.poll_interval_seconds)

    maker_book = _cancel_all(maker_book, logger, "session_end", "maker")
    near_book = _cancel_all(near_book, logger, "session_end", "near_resolution")
    mids = _mids(prev)
    maker_port.record_equity(mids)
    near_port.record_equity(mids)
    state = {
        "source": client.source_name,
        "dry_run": True,
        "live": False,
        "ticks": n_ticks,
        "markets": [s.slug for s in chosen],
        "maker": maker_port.to_dict(mids),
        "near_resolution": near_port.to_dict(mids),
    }
    logger.log("paper_end", live=False, maker_pnl=state["maker"]["net_pnl"], near_pnl=state["near_resolution"]["net_pnl"])
    return state
