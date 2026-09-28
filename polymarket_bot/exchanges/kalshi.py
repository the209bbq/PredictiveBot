"""Kalshi Trade API v2 adapter (public market data + optional demo orders)."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from polymarket_bot.config import AppConfig
from polymarket_bot.exchanges.kalshi_auth import (
    access_headers,
    load_demo_credentials,
    signing_path,
)
from polymarket_bot.guard import (
    DemoOrderError,
    assert_kalshi_demo_orders_allowed,
    is_kalshi_production_url,
    refuse_live_call,
)
from polymarket_bot.market_data import BookLevel, MarketSnapshot, as_datetime, as_decimal
from polymarket_bot.market_data.normalize import depth_from_levels, hours_to_resolution

ONE = Decimal("1")


def _levels_from_pairs(rows: Any) -> list[BookLevel]:
    out: list[BookLevel] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = as_decimal(row[0])
        qty = as_decimal(row[1])
        if price is None or qty is None:
            continue
        out.append(BookLevel(price=price, qty=qty))
    return out


def snapshot_from_kalshi(
    market: dict[str, Any],
    book: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    tick_size_fallback: Decimal = Decimal("0.01"),
) -> MarketSnapshot:
    ticker = str(market.get("slug") or market.get("ticker") or "")
    md = (book or {}).get("orderbook_fp") or book or {}
    yes_bids = _levels_from_pairs(md.get("yes_dollars"))
    no_bids = _levels_from_pairs(md.get("no_dollars"))
    yes_bids.sort(key=lambda lvl: lvl.price, reverse=True)
    # YES ask = 1 − NO bid
    yes_asks = [BookLevel(price=ONE - lvl.price, qty=lvl.qty) for lvl in no_bids if lvl.price <= ONE]
    yes_asks.sort(key=lambda lvl: lvl.price)

    best_bid = yes_bids[0].price if yes_bids else as_decimal(market.get("yes_bid_dollars"))
    best_ask = yes_asks[0].price if yes_asks else as_decimal(market.get("yes_ask_dollars"))
    if best_bid is not None and best_bid <= 0:
        best_bid = None
    if best_ask is not None and best_ask <= 0:
        best_ask = None

    mid = spread = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

    volume = as_decimal(market.get("volume_24h_fp")) or as_decimal(market.get("volume_fp"))
    end_date = as_datetime(market.get("close_time") or market.get("expected_expiration_time"))
    question = str(
        market.get("event_title")
        or market.get("title")
        or market.get("yes_sub_title")
        or ticker
    )
    status = str(market.get("status") or "active")
    if status.lower() in {"active", "open"}:
        status = "open"
    fee_mult = as_decimal(market.get("fee_multiplier")) or Decimal("1")
    return MarketSnapshot(
        slug=ticker,
        question=question,
        category=market.get("category"),
        status=status,
        end_date=end_date,
        hours_to_resolution=hours_to_resolution(end_date, now),
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        last_trade=as_decimal(market.get("last_price_dollars")),
        volume_shares=volume,
        open_interest=as_decimal(market.get("open_interest_fp")),
        notional_traded=as_decimal(market.get("liquidity_dollars")),
        bid_depth_contracts=depth_from_levels(yes_bids),
        ask_depth_contracts=depth_from_levels(yes_asks),
        tick_size=tick_size_fallback,
        fee_coefficient=Decimal("0.07"),
        bids=yes_bids,
        asks=yes_asks,
        raw={"market": market, "book": book},
        venue="kalshi",
        event_title=market.get("event_title"),
        fee_type=str(market.get("fee_type") or "quadratic"),
        fee_multiplier=fee_mult,
    )


class KalshiClient:
    venue = "kalshi"

    def __init__(self, config: AppConfig, *, data_base_url: str | None = None) -> None:
        self.config = config
        self.data_base_url = (data_base_url or config.kalshi.market_data_base_url).rstrip("/")
        self.demo_base_url = config.kalshi.demo_base_url.rstrip("/")
        self._interval = config.kalshi.min_request_interval_seconds
        self._last = 0.0
        self._http = httpx.Client(
            timeout=config.api.request_timeout_seconds,
            headers={"User-Agent": "PredictiveBot/0.2 (read-only)"},
        )
        self.source_name = f"Kalshi Trade API v2 ({self.data_base_url}, unauthenticated market data)"

    def _pace(self) -> None:
        now = time.monotonic()
        wait = self._interval - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{base}{path}"
        delay = 0.25
        last_exc: Exception | None = None
        for _ in range(max(1, self.config.api.max_retries + 2)):
            self._pace()
            try:
                response = self._http.get(url, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if response.status_code == 429:
                # Docs: 429 has no Retry-After. Exponential backoff.
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Kalshi GET {path} failed after retries: {last_exc}")

    def list_markets(
        self,
        *,
        limit: int,
        active: bool = True,
        closed: bool = False,
        offset: int = 0,
        series_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": min(limit, 1000), "mve_filter": "exclude"}
        if active and not closed:
            params["status"] = "open"
        if series_ticker:
            params["series_ticker"] = series_ticker
        payload = self._get(self.data_base_url, "/markets", params)
        rows = []
        for market in payload.get("markets") or []:
            market = dict(market)
            market["slug"] = market.get("ticker")
            rows.append(market)
        return rows[offset : offset + limit]

    def book(self, slug: str) -> dict[str, Any]:
        return self._get(self.data_base_url, f"/markets/{slug}/orderbook", {"depth": 10})

    def snapshot(
        self,
        market: dict[str, Any],
        book: dict[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> MarketSnapshot:
        return snapshot_from_kalshi(
            market,
            book,
            now=now,
            tick_size_fallback=self.config.kalshi.tick_size,
        )

    def trading_hours(self) -> dict[str, Any] | None:
        try:
            return self._get(self.data_base_url, "/exchange/schedule")
        except Exception:
            return None

    def place_demo_order(
        self,
        *,
        ticker: str,
        side: str,
        price: str,
        count: str,
        confirm_demo: bool,
    ) -> dict[str, Any]:
        if not confirm_demo:
            raise DemoOrderError("Pass --confirm-demo to place a Kalshi demo order.")
        assert_kalshi_demo_orders_allowed(
            enabled=self.config.kalshi.demo_orders_enabled,
            base_url=self.demo_base_url,
        )
        if is_kalshi_production_url(self.demo_base_url):
            refuse_live_call("kalshi_production_order")
        key_id, private_key = load_demo_credentials()
        return _signed_request(
            base_url=self.demo_base_url,
            method="POST",
            path="/portfolio/events/orders",
            key_id=key_id,
            private_key=private_key,
            body={
                "ticker": ticker,
                "side": side,
                "count": str(count),
                "price": str(price),
                "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "taker_at_cross",
                "post_only": True,
                "client_order_id": str(uuid.uuid4()),
            },
        )

    def close(self) -> None:
        self._http.close()


def _signed_request(
    *,
    base_url: str,
    method: str,
    path: str,
    key_id: str,
    private_key: Any,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sign_path = signing_path(base_url, path)
    headers = access_headers(
        key_id=key_id,
        private_key=private_key,
        method=method,
        sign_path=sign_path,
        timestamp=timestamp,
    )
    headers["User-Agent"] = "PredictiveBot/0.2 (kalshi-demo)"
    if body is not None:
        headers["Content-Type"] = "application/json"
    delay = 0.25
    with httpx.Client(timeout=30.0) as client:
        for _ in range(4):
            response = client.request(
                method,
                base_url.rstrip("/") + "/" + path.lstrip("/"),
                headers=headers,
                json=body,
            )
            if response.status_code == 429:
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if response.status_code >= 400:
                raise DemoOrderError(
                    f"Kalshi demo {method} {path} failed: {response.status_code} {response.text}"
                )
            return response.json()
    raise DemoOrderError("Kalshi demo request failed after 429 retries")
