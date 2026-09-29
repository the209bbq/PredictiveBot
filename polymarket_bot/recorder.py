"""Read-only forward order-book + tape recorder for allowlisted Kalshi series."""

from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.config import AppConfig
from polymarket_bot.logging_utils import json_default
from polymarket_bot.market_data import BookLevel, MarketSnapshot, TapeTrade
from polymarket_bot.scanner import scan_markets
from polymarket_bot.series_filter import maker_universe_ok


def _top_levels(levels: list[BookLevel], n: int) -> list[dict[str, str]]:
    return [{"price": str(lvl.price), "qty": str(lvl.qty)} for lvl in levels[:n]]


def _tape_rows(tape: list[TapeTrade]) -> list[dict[str, Any]]:
    rows = []
    for trade in tape:
        rows.append(
            {
                "price": str(trade.price),
                "qty": str(trade.qty),
                "ts": trade.ts.isoformat() if trade.ts else None,
                "trade_id": trade.trade_id,
            }
        )
    return rows


def candles_from_tape(tape: list[TapeTrade], now: datetime) -> list[dict[str, Any]]:
    buckets: dict[datetime, dict[str, Any]] = {}
    for trade in tape:
        ts = trade.ts or now
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        minute = ts.replace(second=0, microsecond=0)
        bucket = buckets.get(minute)
        if bucket is None:
            buckets[minute] = {
                "ts": minute.isoformat(),
                "open": str(trade.price),
                "high": trade.price,
                "low": trade.price,
                "close": trade.price,
                "volume": trade.qty,
            }
        else:
            bucket["high"] = max(bucket["high"], trade.price)
            bucket["low"] = min(bucket["low"], trade.price)
            bucket["close"] = trade.price
            bucket["volume"] += trade.qty
    rows = []
    for minute in sorted(buckets):
        bucket = buckets[minute]
        rows.append(
            {
                "ts": bucket["ts"],
                "open": bucket["open"],
                "high": str(bucket["high"]),
                "low": str(bucket["low"]),
                "close": str(bucket["close"]),
                "volume": str(bucket["volume"]),
            }
        )
    return rows


def snapshot_row(
    snap: MarketSnapshot,
    *,
    now: datetime,
    levels: int,
    candles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tape = list(snap.tape or [])
    return {
        "ts": now.isoformat(),
        "venue": snap.venue,
        "ticker": snap.slug,
        "question": snap.question,
        "best_bid": str(snap.best_bid) if snap.best_bid is not None else None,
        "best_ask": str(snap.best_ask) if snap.best_ask is not None else None,
        "last_trade": str(snap.last_trade) if snap.last_trade is not None else None,
        "hours_to_resolution": snap.hours_to_resolution,
        "bids": _top_levels(snap.bids, levels),
        "asks": _top_levels(snap.asks, levels),
        "tape": _tape_rows(tape),
        "candles_1m": candles if candles is not None else candles_from_tape(tape, now),
    }


class RotatingWriter:
    def __init__(self, directory: Path, *, rotate_mb: float, max_files: int, fmt: str) -> None:
        self.directory = directory
        self.rotate_bytes = int(rotate_mb * 1024 * 1024)
        self.max_files = max(1, max_files)
        self.fmt = fmt
        self.directory.mkdir(parents=True, exist_ok=True)
        self._fh: gzip.GzipFile | None = None
        self._path: Path | None = None
        self._rows: list[dict[str, Any]] = []

    @property
    def path(self) -> Path | None:
        return self._path

    def _open_jsonl(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._path = self.directory / f"{stamp}.jsonl.gz"
        self._fh = gzip.open(self._path, "at", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        if self._path and self._path.exists() and self._path.stat().st_size >= self.rotate_bytes:
            self.close()
        existing = sorted(self.directory.glob("*.jsonl.gz")) + sorted(self.directory.glob("*.parquet"))
        extra = len(existing) - self.max_files
        if extra > 0:
            for stale in existing[:extra]:
                stale.unlink(missing_ok=True)

    def write(self, row: dict[str, Any]) -> Path:
        self._rotate_if_needed()
        if self.fmt == "parquet":
            self._rows.append(row)
            if self._path is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                self._path = self.directory / f"{stamp}.parquet"
            return self._path
        if self._fh is None:
            self._open_jsonl()
        assert self._fh is not None and self._path is not None
        self._fh.write(json.dumps(row, default=json_default) + "\n")
        self._fh.flush()
        return self._path

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self.fmt == "parquet" and self._rows:
            self._flush_parquet()
            self._rows = []

    def _flush_parquet(self) -> None:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Parquet output requires pyarrow. Use record.format: jsonl.") from exc
        if self._path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self._path = self.directory / f"{stamp}.parquet"
        table = pa.Table.from_pylist(self._rows)
        pq.write_table(table, self._path)


def _refresh(client, snap: MarketSnapshot, now: datetime) -> MarketSnapshot:
    book = client.book(snap.slug)
    market = snap.raw.get("market") or {"ticker": snap.slug, "slug": snap.slug}
    refreshed = client.snapshot(market, book, now=now)
    getter = getattr(client, "trades", None)
    if callable(getter):
        refreshed.tape = getter(snap.slug, limit=1000, max_pages=5)
        if refreshed.tape:
            refreshed.last_trade = refreshed.tape[-1].price
    refreshed.stale = False
    refreshed.book_fetched = True
    return refreshed


def _candles_for(client, snap: MarketSnapshot, now: datetime) -> list[dict[str, Any]]:
    fn = getattr(client, "candlesticks", None)
    if callable(fn):
        try:
            end = int(now.timestamp())
            rows = fn(snap.slug, start_ts=end - 3600, end_ts=end, period_interval=1)
            if rows:
                return rows
        except Exception:
            pass
    return candles_from_tape(list(snap.tape or []), now)


def run_record(
    config: AppConfig,
    client,
    *,
    ticks: int | None = None,
    sleep: bool = True,
    now: datetime | None = None,
) -> Path:
    """Snapshot allowlisted books + tape. Read-only. Never places orders."""
    now = now or datetime.now(timezone.utc)
    writer = RotatingWriter(
        config.record.directory,
        rotate_mb=config.record.rotate_mb,
        max_files=config.record.max_files,
        fmt=config.record.fmt,
    )
    n_ticks = ticks if ticks is not None else 1
    last_path: Path | None = None
    try:
        for i in range(n_ticks):
            tick_now = datetime.now(timezone.utc)
            rows = [
                snap
                for snap in scan_markets(client, config, now=tick_now)
                if maker_universe_ok(snap, config, now=tick_now)
            ]
            for snap in rows:
                try:
                    live = _refresh(client, snap, tick_now)
                except Exception:
                    continue
                last_path = writer.write(
                    snapshot_row(
                        live,
                        now=tick_now,
                        levels=config.record.book_levels,
                        candles=_candles_for(client, live, tick_now),
                    )
                )
            if sleep and i < n_ticks - 1:
                time.sleep(max(0.0, config.record.interval_seconds))
    finally:
        writer.close()
    if last_path is None:
        last_path = writer.path or (config.record.directory / "empty.jsonl.gz")
        if not last_path.exists():
            last_path.write_bytes(b"")
    return last_path
