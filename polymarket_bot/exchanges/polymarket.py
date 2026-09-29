"""Polymarket US adapter (public gateway via official SDK)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from polymarket_bot.config import AppConfig
from polymarket_bot.guard import refuse_live_call
from polymarket_bot.market_data import MarketSnapshot
from polymarket_bot.market_data.normalize import snapshot_from_payloads
from polymarket_bot.market_data.sdk_client import SdkPublicClient


class PolymarketClient(SdkPublicClient):
    venue = "polymarket_us"

    def snapshot(
        self,
        market: dict[str, Any],
        book: dict[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> MarketSnapshot:
        snap = snapshot_from_payloads(
            market,
            book,
            None,
            now=now,
            tick_size_fallback=self._tick,
        )
        snap.venue = "polymarket_us"
        snap.event_title = str(market.get("question") or market.get("title") or snap.question)
        snap.fee_type = "polymarket"
        snap.status = "open" if "OPEN" in (snap.status or "").upper() or market.get("active") else snap.status
        return snap

    def trading_hours(self) -> dict[str, Any] | None:
        return None

    def place_demo_order(self, **kwargs: Any) -> dict[str, Any]:
        refuse_live_call("polymarket_us_order")
        raise AssertionError("unreachable")

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._tick = config.paper.tick_size_fallback
        self.venue = "polymarket_us"
