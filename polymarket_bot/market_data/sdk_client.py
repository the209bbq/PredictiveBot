"""Public market data via the official polymarket-us SDK (unauthenticated)."""

from __future__ import annotations

import time
from typing import Any

from polymarket_us import PolymarketUS

from polymarket_bot.config import AppConfig
from polymarket_bot.guard import refuse_live_call


class _DisabledTrading:
    def __getattr__(self, name: str) -> None:
        refuse_live_call(f"sdk.{name}")
        raise AssertionError("unreachable")

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        refuse_live_call("sdk()")


class SdkPublicClient:
    """Read-only wrapper. Trading resources on the SDK are replaced with stubs."""

    source_name = "polymarket-us SDK (gateway.polymarket.us, unauthenticated)"

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
        self._pace()
        return dict(self._sdk.markets.book(slug))

    def bbo(self, slug: str) -> dict[str, Any]:
        self._pace()
        return dict(self._sdk.markets.bbo(slug))

    def close(self) -> None:
        self._sdk.close()
