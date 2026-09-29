"""ReplayClient that steps through recorded ticks."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ReplayClient:
    source_name = "fixture replay"
    venue = "polymarket_us"

    def __init__(self, path: str | Path, now: datetime | None = None) -> None:
        self.path = Path(path)
        payload = json.loads(self.path.read_text())
        self.venue = str(payload.get("venue") or "polymarket_us")
        self._ticks: list[dict[str, Any]] = payload["ticks"]
        self._cursor = 0
        self._now = now or datetime.now(timezone.utc)
        self._items: list[dict[str, Any]] = []
        self._by_slug: dict[str, dict[str, Any]] = {}
        self._load(0)

    def _load(self, index: int) -> None:
        raw_items = deepcopy(self._ticks[index]["items"])
        self._items = [self._relativize(item) for item in raw_items]
        self._by_slug = {(item.get("market") or item)["slug"]: item for item in self._items}

    def _relativize(self, item: dict[str, Any]) -> dict[str, Any]:
        market = dict(item.get("market") or item)
        hours = market.get("hoursFromNow")
        if hours is not None:
            iso = (self._now + timedelta(hours=float(hours))).isoformat()
            market["endDate"] = iso
            market["close_time"] = iso
            market["active"] = True
            market["closed"] = False
            market["status"] = "active"
        item = dict(item)
        item["market"] = market
        return item

    def list_markets(
        self,
        *,
        limit: int,
        active: bool = True,
        closed: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = [item["market"] for item in self._items]
        return rows[offset : offset + limit]

    def book(self, slug: str) -> dict[str, Any]:
        return self._by_slug[slug]["book"]

    def last_trade(self, slug: str):
        from polymarket_bot.market_data import as_decimal

        item = self._by_slug[slug]
        market = item.get("market") or {}
        book = item.get("book") or {}
        md = book.get("marketData") or {}
        stats = md.get("stats") or {}
        return (
            as_decimal(market.get("last_price_dollars"))
            or as_decimal(stats.get("lastTradePx"))
            or as_decimal(market.get("lastTradePrice"))
        )

    def bbo(self, slug: str) -> dict[str, Any]:
        item = self._by_slug[slug]
        return item.get("bbo") or {}

    def snapshot(self, market, book=None, *, now=None):
        now = now or self._now
        if self.venue == "kalshi" or "orderbook_fp" in (book or {}):
            from polymarket_bot.exchanges.kalshi import snapshot_from_kalshi

            return snapshot_from_kalshi(market, book, now=now)
        from polymarket_bot.market_data.normalize import snapshot_from_payloads

        snap = snapshot_from_payloads(market, book, None, now=now)
        snap.venue = "polymarket_us"
        snap.event_title = snap.question
        snap.fee_type = "polymarket"
        return snap

    def trading_hours(self):
        return None

    def advance(self) -> bool:
        nxt = self._cursor + 1
        if nxt >= len(self._ticks):
            return False
        self._cursor = nxt
        self._load(nxt)
        return True

    @property
    def tick_count(self) -> int:
        return len(self._ticks)

    def close(self) -> None:
        return None
