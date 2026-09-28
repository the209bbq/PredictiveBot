"""Normalized market snapshots used by the scanner and paper engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol


def as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict) and "value" in value:
        return as_decimal(value["value"])
    inner = getattr(value, "value", None)
    if inner is not None and inner is not value:
        return as_decimal(inner)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        if text == "" or text.lower() == "none":
            return None
        return Decimal(text)
    return None


def as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    qty: Decimal


@dataclass
class MarketSnapshot:
    slug: str
    question: str
    category: str | None
    status: str | None
    end_date: datetime | None
    hours_to_resolution: float | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    spread: Decimal | None
    last_trade: Decimal | None
    volume_shares: Decimal | None
    open_interest: Decimal | None
    notional_traded: Decimal | None
    bid_depth_contracts: Decimal
    ask_depth_contracts: Decimal
    tick_size: Decimal
    fee_coefficient: Decimal | None
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    venue: str = "polymarket_us"
    event_title: str | None = None
    fee_type: str | None = None
    fee_multiplier: Decimal = Decimal("1")
    stale: bool = False
    book_fetched: bool = False

    @property
    def liquid(self) -> bool:
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.spread is not None
            and self.spread >= 0
        )


class MarketDataClient(Protocol):
    source_name: str
    venue: str

    def list_markets(
        self,
        *,
        limit: int,
        active: bool = True,
        closed: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        ...

    def book(self, slug: str) -> dict[str, Any]:
        ...

    def snapshot(
        self,
        market: dict[str, Any],
        book: dict[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> MarketSnapshot:
        ...

    def trading_hours(self) -> dict[str, Any] | None:
        ...

    def close(self) -> None:
        ...
