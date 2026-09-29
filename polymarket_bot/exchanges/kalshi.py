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
from polymarket_bot.market_data import BookLevel, MarketSnapshot, TapeTrade, as_datetime, as_decimal
from polymarket_bot.market_data.errors import BookFetchError
from polymarket_bot.market_data.normalize import depth_from_levels, hours_to_resolution

_RETRY_STATUSES = {429, 500, 502, 503, 504}
CLIENT_ORDER_PREFIX = "pmbot-"

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
    close_date = as_datetime(market.get("close_time"))
    event_date = as_datetime(market.get("expected_expiration_time"))
    kickoff = as_datetime(market.get("occurrence_datetime"))
    # Official close is often hours after the event. Use the earliest of
    # kickoff / expected expiration / close so live games are not treated as far-dated.
    candidates = [dt for dt in (kickoff, event_date, close_date) if dt is not None]
    end_date = min(candidates) if candidates else None
    list_bid_sz = as_decimal(market.get("yes_bid_size_fp")) or Decimal("0")
    list_ask_sz = as_decimal(market.get("yes_ask_size_fp")) or Decimal("0")
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
        bid_depth_contracts=depth_from_levels(yes_bids) if yes_bids else list_bid_sz,
        ask_depth_contracts=depth_from_levels(yes_asks) if yes_asks else list_ask_sz,
        tick_size=tick_size_fallback,
        fee_coefficient=Decimal("0.07"),
        bids=yes_bids,
        asks=yes_asks,
        raw={"market": market, "book": book},
        venue="kalshi",
        event_title=market.get("event_title"),
        fee_type=str(market.get("fee_type") or "quadratic"),
        fee_multiplier=fee_mult,
        stale=False,
        book_fetched=bool(yes_bids or yes_asks),
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
        self._bot_order_ids: set[str] = set()

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
        attempts = max(1, self.config.api.max_retries + 2)
        for _ in range(attempts):
            self._pace()
            try:
                response = self._http.get(url, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if response.status_code in _RETRY_STATUSES:
                last_exc = RuntimeError(f"HTTP {response.status_code}")
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            try:
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_exc = exc
                break
        raise BookFetchError(f"Kalshi GET {path} failed after retries: {last_exc}")

    def list_markets(
        self,
        *,
        limit: int,
        active: bool = True,
        closed: bool = False,
        offset: int = 0,
        series_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        page_size = min(1000, max(1, getattr(self.config.scanner, "list_page_size", 200)))
        max_pages = max(1, getattr(self.config.scanner, "max_list_pages", 6))
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while len(rows) < offset + limit and pages < max_pages:
            params: dict[str, Any] = {
                "limit": min(page_size, offset + limit - len(rows)),
                "mve_filter": "exclude",
            }
            if active and not closed:
                params["status"] = "open"
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            payload = self._get(self.data_base_url, "/markets", params)
            pages += 1
            batch = payload.get("markets") or []
            for market in batch:
                market = dict(market)
                market["slug"] = market.get("ticker")
                rows.append(market)
            cursor = payload.get("cursor") or None
            if not batch or not cursor:
                break
        return rows[offset : offset + limit]

    def book(self, slug: str) -> dict[str, Any]:
        return self._get(self.data_base_url, f"/markets/{slug}/orderbook", {"depth": 10})

    def last_trade(self, slug: str) -> Decimal | None:
        tape = self.trades(slug, limit=1, max_pages=1)
        return tape[-1].price if tape else None

    def trades(
        self,
        slug: str,
        *,
        min_ts: int | float | None = None,
        limit: int = 200,
        max_pages: int = 5,
    ) -> list[TapeTrade]:
        """Full public tape since min_ts (unix seconds). Newest pages first, returned oldest-first."""
        out: list[TapeTrade] = []
        cursor: str | None = None
        pages = 0
        seen: set[str] = set()
        while pages < max_pages and len(out) < limit:
            params: dict[str, Any] = {
                "ticker": slug,
                "limit": min(1000, max(1, limit - len(out))),
                "is_block_trade": False,
            }
            if min_ts is not None:
                params["min_ts"] = int(min_ts)
            if cursor:
                params["cursor"] = cursor
            payload = self._get(self.data_base_url, "/markets/trades", params)
            pages += 1
            rows = payload.get("trades") or []
            for row in rows:
                price = as_decimal(row.get("yes_price_dollars") or row.get("yes_price"))
                if price is None:
                    continue
                qty = (
                    as_decimal(row.get("count"))
                    or as_decimal(row.get("contracts"))
                    or as_decimal(row.get("quantity"))
                    or Decimal("1")
                )
                ts = as_datetime(row.get("created_time") or row.get("ts"))
                tid = row.get("trade_id") or row.get("id")
                key = str(tid or f"{price}:{qty}:{ts}")
                if key in seen:
                    continue
                seen.add(key)
                out.append(TapeTrade(price=price, qty=qty, ts=ts, trade_id=str(tid) if tid else None))
            cursor = payload.get("cursor") or None
            if not rows or not cursor:
                break
        out.sort(key=lambda t: (t.ts or datetime.min.replace(tzinfo=timezone.utc), t.trade_id or ""))
        return out

    def candlesticks(
        self,
        slug: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        period_interval: int = 1,
    ) -> list[dict[str, Any]]:
        """1-minute candles. Tries documented paths; empty list if unavailable."""
        end_ts = int(end_ts if end_ts is not None else datetime.now(timezone.utc).timestamp())
        start_ts = int(start_ts if start_ts is not None else end_ts - 3600)
        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        attempts = (
            (f"/markets/{slug}/candlesticks", params),
            (
                "/markets/candlesticks",
                {**params, "ticker": slug, "market_ticker": slug},
            ),
        )
        for path, query in attempts:
            try:
                payload = self._get(self.data_base_url, path, query)
            except BookFetchError:
                continue
            rows = payload.get("candlesticks") or payload.get("candles") or []
            out: list[dict[str, Any]] = []
            for row in rows:
                price = row.get("price") if isinstance(row.get("price"), dict) else row
                ts = row.get("end_period_ts") or row.get("end_ts") or row.get("ts")
                out.append(
                    {
                        "ts": ts,
                        "open": str(price.get("open") or row.get("open") or ""),
                        "high": str(price.get("high") or row.get("high") or ""),
                        "low": str(price.get("low") or row.get("low") or ""),
                        "close": str(price.get("close") or row.get("close") or ""),
                        "volume": str(row.get("volume") or row.get("yes_volume") or "0"),
                    }
                )
            if out:
                return out
        return []

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

    def _assert_demo(self, confirm_demo: bool) -> None:
        if not confirm_demo:
            raise DemoOrderError("Pass --confirm-demo to place Kalshi demo orders.")
        assert_kalshi_demo_orders_allowed(
            enabled=self.config.kalshi.demo_orders_enabled,
            base_url=self.demo_base_url,
        )
        if is_kalshi_production_url(self.demo_base_url):
            refuse_live_call("kalshi_production_order")

    def signed_demo(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        confirm_demo: bool = True,
    ) -> dict[str, Any]:
        self._assert_demo(confirm_demo)
        key_id, private_key = load_demo_credentials()
        return signed_request(
            base_url=self.demo_base_url,
            method=method,
            path=path,
            key_id=key_id,
            private_key=private_key,
            body=body,
            params=params,
            max_retries=self.config.api.max_retries + 3,
        )

    def place_demo_order(
        self,
        *,
        ticker: str,
        side: str,
        price: str,
        count: str,
        confirm_demo: bool,
        post_only: bool = True,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        cid = client_order_id or f"{CLIENT_ORDER_PREFIX}{uuid.uuid4().hex}"
        result = self.signed_demo(
            "POST",
            "/portfolio/events/orders",
            confirm_demo=confirm_demo,
            body={
                "ticker": ticker,
                "side": side,
                "count": str(count),
                "price": str(price),
                "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "taker_at_cross",
                "post_only": post_only,
                "client_order_id": cid,
            },
        )
        order = result.get("order") if isinstance(result.get("order"), dict) else result
        oid = order.get("order_id") or order.get("id")
        if oid:
            self._bot_order_ids.add(str(oid))
        return result

    def cancel_demo_order(self, order_id: str, *, ticker: str | None, confirm_demo: bool) -> dict[str, Any]:
        params = {"market_ticker": ticker} if ticker else None
        return self.signed_demo(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            confirm_demo=confirm_demo,
            params=params,
        )

    def cancel_all_demo_orders(self, *, confirm_demo: bool) -> dict[str, Any]:
        """Emergency only: DELETE /portfolio/events/orders — every resting order on the account."""
        return self.signed_demo("DELETE", "/portfolio/events/orders", confirm_demo=confirm_demo)

    def is_bot_order(self, row: dict[str, Any]) -> bool:
        cid = str(row.get("client_order_id") or "")
        oid = str(row.get("order_id") or row.get("id") or "")
        if cid.startswith(CLIENT_ORDER_PREFIX):
            return True
        return bool(oid and oid in self._bot_order_ids)

    def list_bot_demo_orders(self, *, status: str = "resting", confirm_demo: bool) -> list[dict[str, Any]]:
        return [row for row in self.list_demo_orders(status=status, confirm_demo=confirm_demo) if self.is_bot_order(row)]

    def cancel_bot_demo_orders(self, *, confirm_demo: bool) -> dict[str, Any]:
        """Cancel only orders this bot placed (client_order_id prefix or tracked IDs)."""
        mine = self.list_bot_demo_orders(status="resting", confirm_demo=confirm_demo)
        batch = [
            {"order_id": str(row.get("order_id") or row.get("id")), "market_ticker": str(row.get("ticker") or "")}
            for row in mine
            if row.get("order_id") or row.get("id")
        ]
        if not batch:
            return {"cancelled": 0}
        try:
            self.batch_cancel_demo_orders(batch, confirm_demo=confirm_demo)
        except DemoOrderError:
            for item in batch:
                try:
                    self.cancel_demo_order(item["order_id"], ticker=item.get("market_ticker"), confirm_demo=confirm_demo)
                except DemoOrderError:
                    continue
        return {"cancelled": len(batch)}

    def batch_cancel_demo_orders(
        self,
        orders: list[dict[str, str]],
        *,
        confirm_demo: bool,
    ) -> dict[str, Any]:
        return self.signed_demo(
            "DELETE",
            "/portfolio/events/orders/batched",
            confirm_demo=confirm_demo,
            body={"orders": orders},
        )

    def list_demo_orders(self, *, status: str = "resting", confirm_demo: bool) -> list[dict[str, Any]]:
        payload = self.signed_demo(
            "GET",
            "/portfolio/orders",
            confirm_demo=confirm_demo,
            params={"status": status, "limit": 200},
        )
        return list(payload.get("orders") or [])

    def demo_balance(self, *, confirm_demo: bool) -> dict[str, Any]:
        return self.signed_demo("GET", "/portfolio/balance", confirm_demo=confirm_demo)

    def demo_positions(self, *, confirm_demo: bool) -> dict[str, Any]:
        return self.signed_demo("GET", "/portfolio/positions", confirm_demo=confirm_demo)

    def demo_fills(self, *, confirm_demo: bool, limit: int = 100) -> list[dict[str, Any]]:
        payload = self.signed_demo(
            "GET",
            "/portfolio/fills",
            confirm_demo=confirm_demo,
            params={"limit": limit},
        )
        return list(payload.get("fills") or payload.get("market_positions") or [])

    def shutdown_demo_orders(
        self,
        *,
        confirm_demo: bool,
        emergency_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Cancel this bot's resting orders (or the whole account if emergency_all)."""
        last_error = None
        for _ in range(4):
            try:
                if emergency_all:
                    self.cancel_all_demo_orders(confirm_demo=confirm_demo)
                else:
                    self.cancel_bot_demo_orders(confirm_demo=confirm_demo)
                last_error = None
                break
            except DemoOrderError as exc:
                last_error = exc
                time.sleep(1.0)
        remaining = (
            self.list_demo_orders(status="resting", confirm_demo=confirm_demo)
            if emergency_all
            else self.list_bot_demo_orders(status="resting", confirm_demo=confirm_demo)
        )
        if remaining and not emergency_all:
            batch = [
                {"order_id": row.get("order_id"), "market_ticker": row.get("ticker")}
                for row in remaining
                if row.get("order_id")
            ]
            if batch:
                try:
                    self.batch_cancel_demo_orders(batch, confirm_demo=confirm_demo)
                except DemoOrderError as exc:
                    last_error = exc
                remaining = self.list_bot_demo_orders(status="resting", confirm_demo=confirm_demo)
        if remaining:
            raise DemoOrderError(
                "ALERT: resting Kalshi DEMO orders this bot placed remain after cancel. "
                f"Count={len(remaining)} last_error={last_error} "
                f"ids={[row.get('order_id') for row in remaining]}"
            )
        return remaining

    def close(self) -> None:
        self._http.close()


def signed_request(
    *,
    base_url: str,
    method: str,
    path: str,
    key_id: str,
    private_key: Any,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    max_retries: int = 6,
) -> dict[str, Any]:
    """Signed Kalshi call. Retries 429 and 5xx with a fresh timestamp each try."""
    delay = 0.25
    last_error = "unknown"
    with httpx.Client(timeout=30.0) as client:
        for _ in range(max(1, max_retries)):
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
            try:
                response = client.request(
                    method,
                    base_url.rstrip("/") + "/" + path.lstrip("/"),
                    headers=headers,
                    json=body,
                    params=params,
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if response.status_code in _RETRY_STATUSES:
                last_error = f"{response.status_code} {response.text[:200]}"
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if response.status_code == 204:
                return {}
            if response.status_code >= 400:
                raise DemoOrderError(
                    f"Kalshi demo {method} {path} failed: {response.status_code} {response.text}"
                )
            if not response.content:
                return {}
            return response.json()
    raise DemoOrderError(f"Kalshi demo {method} {path} failed after retries: {last_error}")
