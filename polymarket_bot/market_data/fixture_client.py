"""Recorded/fixture market data with the same interface as the live SDK client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FixtureClient:
    source_name = "recorded fixtures"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        payload = json.loads(self.path.read_text())
        self._items = payload.get("items") or payload.get("markets") or []
        self._by_slug: dict[str, dict[str, Any]] = {}
        for item in self._items:
            market = item.get("market") or item
            slug = market["slug"]
            self._by_slug[slug] = item

    def list_markets(
        self,
        *,
        limit: int,
        active: bool = True,
        closed: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = []
        for item in self._items:
            market = item.get("market") or item
            if active and not market.get("active", True):
                continue
            if not closed and market.get("closed"):
                continue
            rows.append(market)
        return rows[offset : offset + limit]

    def book(self, slug: str) -> dict[str, Any]:
        item = self._by_slug[slug]
        if "book" in item:
            return item["book"]
        return {"marketData": item.get("marketData") or item}

    def bbo(self, slug: str) -> dict[str, Any]:
        item = self._by_slug[slug]
        if "bbo" in item:
            return item["bbo"]
        book = self.book(slug)
        md = book.get("marketData") or {}
        stats = md.get("stats") or {}
        bids = md.get("bids") or []
        asks = md.get("offers") or md.get("asks") or []
        return {
            "marketData": {
                "marketSlug": slug,
                "bestBid": bids[0]["px"] if bids else None,
                "bestAsk": asks[0]["px"] if asks else None,
                "lastTradePx": stats.get("lastTradePx"),
                "sharesTraded": stats.get("sharesTraded"),
                "openInterest": stats.get("openInterest"),
                "bidShares": str(sum(float(b.get("qty") or 0) for b in bids)),
                "askShares": str(sum(float(a.get("qty") or 0) for a in asks)),
                "state": md.get("state"),
            }
        }

    def close(self) -> None:
        return None
