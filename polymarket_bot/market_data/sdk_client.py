"""Public market data via the official polymarket-us SDK (unauthenticated)."""

from __future__ import annotations

import time
from typing import Any

from polymarket_us import PolymarketUS

from polymarket_bot.config import AppConfig
from polymarket_bot.guard import refuse_live_call
from polymarket_bot.market_data.errors import BookFetchError


class _DisabledTrading:
    def __getattr__(self, name: str) -> None:
        refuse_live_call(f"sdk.{name}")
        raise AssertionError("unreachable")

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        refuse_live_call("sdk()")


class SdkPublicClient:
    """Read-only wrapper. Trading resources on the SDK are replaced with stubs."""

    source_name = "polymarket-us SDK (gateway.polymarket.us, unauthenticated)"
    venue = "polymarket_us"

    def __init__(self, config: AppConfig) -> None:
        self._interval = config.api.min_request_interval_seconds
        self._last_request = 0.0
        self._sdk = PolymarketUS(
            key_id=None,
            secret_key=None,
            gateway_base_url=config.api.gateway_base_url,
            api_base_url=config.api.api_base_url,
            timeout=config.api.request_timeout_seconds,
            max_retries=config.api.max_retries,
        )
        # Belt and suspenders: even if a future caller grabs the inner SDK,
        # order/account/portfolio methods cannot be used in this version.
        self._sdk.orders = _DisabledTrading()  # type: ignore[assignment]
        self._sdk.account = _DisabledTrading()  # type: ignore[assignment]
        self._sdk.portfolio = _DisabledTrading()  # type: ignore[assignment]
        self._sdk.ws = _DisabledTrading()  # type: ignore[assignment]

    def _pace(self) -> None:
        now = time.monotonic()
        wait = self._interval - (now - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def list_markets(
        self,
        *,
        limit: int,
        active: bool = True,
        closed: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._pace()
        params: dict[str, Any] = {"limit": limit, "offset": offset, "active": active}
        if not closed:
            params["closed"] = False
        payload = self._sdk.markets.list(params)
        return list(payload.get("markets") or [])

    def book(self, slug: str) -> dict[str, Any]:
        delay = 0.5
        last_exc: Exception | None = None
        for _ in range(max(1, 4)):
            self._pace()
            try:
                return dict(self._sdk.markets.book(slug))
            except Exception as exc:
                last_exc = exc
                text = str(exc).lower()
                retryable = "429" in text or "503" in text or "502" in text or "500" in text or "timeout" in text
                if not retryable:
                    raise BookFetchError(f"Polymarket US book {slug} failed: {exc}") from exc
                time.sleep(delay)
                delay = min(delay * 2, 8)
        raise BookFetchError(f"Polymarket US book {slug} failed after retries: {last_exc}")

    def bbo(self, slug: str) -> dict[str, Any]:
        self._pace()
        return dict(self._sdk.markets.bbo(slug))

    def snapshot(self, market, book=None, *, now=None):
        from polymarket_bot.market_data.normalize import snapshot_from_payloads

        snap = snapshot_from_payloads(market, book, None, now=now)
        snap.venue = "polymarket_us"
        snap.event_title = snap.question
        snap.fee_type = "polymarket"
        return snap

    def trading_hours(self):
        return None

    def close(self) -> None:
        self._sdk.close()
