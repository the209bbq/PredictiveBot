"""ReplayClient that steps through recorded ticks."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ReplayClient:
    source_name = "fixture replay"

    def __init__(self, path: str | Path, now: datetime | None = None) -> None:
        self.path = Path(path)
        payload = json.loads(self.path.read_text())
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
            market["endDate"] = (self._now + timedelta(hours=float(hours))).isoformat()
            market["active"] = True
            market["closed"] = False
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

    def bbo(self, slug: str) -> dict[str, Any]:
        return self._by_slug[slug]["bbo"]

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
