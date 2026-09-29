"""Maker-universe series allow/deny, event blackouts, and fee-flag skip.

The allowlist feeds the maker gate. It does not replace the per-market risk score.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from polymarket_bot.config import AppConfig
from polymarket_bot.market_data import MarketSnapshot

PT = ZoneInfo("America/Los_Angeles")

DEFAULT_ALLOW = (
    "KXHIGH",
    "KXBRENTW",
    "KXRT",
    "KXU3",
    "KXPAYROLLS",
    "KXDEMO",
)
DEFAULT_DISABLED = (
    "KXTRUTHSOCIAL",
    "KXBTC",
    "KXETHD",
    "KXAAAGASD",
    "KXAAAGASW",
)
DEFAULT_DENY = (
    "KXNFL",
    "KXNBA",
    "KXMLB",
    "KXNHL",
    "KXNCAA",
    "KXWNBA",
    "KXGAME",
    "KXFEDDECISION",
    "KXFED",
    "KXCPI",
    "CPI",
    "KXWTI",
    "KXINXU",
    "KXBTC15M",
    "KXBTCD",
    "KXXRPD",
    "KXRAIN",
    "KXEPL",
    "KXSOCCER",
)
_SPORTS_DENY = ("GAME", "NFL", "NBA", "MLB", "NHL", "NCAA", "WNBA", "EPL")
_MENTION = ("mention", "will say", "will x say", "say that")
_ENTERTAIN = ("ranking", "box office", "oscar", "grammy", "emmy", "rotten tomatoes")
_MAKER_FEE = ("quadratic_with_maker", "maker_fee")


def series_ticker(snap: MarketSnapshot) -> str:
    market = (snap.raw or {}).get("market") or {}
    return str(market.get("series_ticker") or snap.slug or "").upper()


def _prefixes(config: AppConfig) -> tuple[list[str], list[str]]:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    allow = [str(x).upper() for x in (extra.get("allow") or DEFAULT_ALLOW)]
    deny = [str(x).upper() for x in (extra.get("deny") or DEFAULT_DENY)]
    return allow, deny


def _starts_with_any(text: str, prefixes: list[str] | tuple[str, ...]) -> bool:
    return any(text.startswith(p) or p in text for p in prefixes if p)


def filter_enabled(config: AppConfig) -> bool:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    if "enabled" in extra:
        return bool(extra.get("enabled"))
    return True


def applies_to(snap: MarketSnapshot) -> bool:
    slug = (snap.slug or "").upper()
    if snap.venue == "polymarket_us":
        return False
    if slug.startswith("DEMO-") or slug in {"M", "FAV"}:
        return False
    series = series_ticker(snap)
    return series.startswith("KX") or slug.startswith("KX")


def _is_denied(snap: MarketSnapshot, deny: list[str]) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    blob = " ".join(
        filter(None, [snap.slug, snap.question, snap.event_title, snap.category, series])
    ).lower()
    for prefix in deny:
        if series.startswith(prefix) or slug.startswith(prefix):
            return True
    if any(tok.lower() in series or tok.lower() in slug for tok in _SPORTS_DENY):
        return True
    if any(tok in blob for tok in _MENTION):
        return True
    if any(tok in blob for tok in _ENTERTAIN):
        return True
    return False


def _is_allowed(snap: MarketSnapshot, allow: list[str]) -> bool:
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    for prefix in allow:
        if series.startswith(prefix) or slug.startswith(prefix):
            if prefix == "KXBTC" and (series.startswith("KXBTC15M") or series.startswith("KXBTCD") or slug.startswith("KXBTC15M") or slug.startswith("KXBTCD")):
                continue
            return True
    return False


def _kxhigh_ok(snap: MarketSnapshot) -> bool:
    hours = snap.hours_to_resolution
    if hours is None:
        return False
    # Next-day plus late-morning on the resolution day. Skip late-day same-session.
    return 4.0 <= hours <= 40.0


def maker_min_hours(snap: MarketSnapshot, config: AppConfig) -> float:
    """Per-series hours floor. Default 24h; KXHIGH/gas override to 0."""
    series = series_ticker(snap)
    slug = (snap.slug or "").upper()
    overrides = dict(config.paper.risk.maker_min_hours_overrides or {})
    for prefix, hours in sorted(overrides.items(), key=lambda kv: -len(str(kv[0]))):
        token = str(prefix).upper()
        if series.startswith(token) or slug.startswith(token):
            return float(hours)
    return float(config.paper.risk.maker_min_hours_to_resolution)


def _kxrt_ok(snap: MarketSnapshot) -> bool:
    mid = snap.mid
    if mid is None:
        return False
    return Decimal("0.02") < mid < Decimal("0.98")


def _has_maker_fees(snap: MarketSnapshot) -> bool:
    fee = (snap.fee_type or "").lower()
    return any(tok in fee for tok in _MAKER_FEE)


def _in_blackout(now: datetime, at: datetime, minutes: int) -> bool:
    delta = abs((now - at).total_seconds())
    return delta <= minutes * 60


def event_blackout(snap: MarketSnapshot, config: AppConfig, now: datetime) -> bool:
    extra = (config.extra.get("paper") or {}).get("series") or {}
    minutes = int(extra.get("event_blackout_minutes", 30))
    events = extra.get("events") or [
        {"name": "jobs", "at": "2026-10-02T12:30:00+00:00", "series": ["KXHIGH", "KXRT", "KXBRENTW", "KXU3", "KXPAYROLLS"]},
        {"name": "cpi", "at": "2026-10-14T12:30:00+00:00", "series": ["KXRT", "KXBTC", "KXETHD"]},
        {"name": "fomc", "at": "2026-10-28T18:00:00+00:00", "series": ["KXFED", "KXFEDDECISION"]},
    ]
    series = series_ticker(snap)
    for event in events:
        raw_at = event.get("at")
        if not raw_at:
            continue
        try:
            when = datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if not _in_blackout(now, when, minutes):
            continue
        prefixes = [str(x).upper() for x in (event.get("series") or [])]
        if not prefixes or _starts_with_any(series, prefixes):
            return True
    aaa_on = bool(extra.get("aaa_blackout_enabled", False))
    aaa_start = extra.get("aaa_blackout_start")
    if aaa_on and aaa_start and series.startswith("KXAAA"):
        try:
            hh, mm = str(aaa_start).split(":")
            local = now.astimezone(PT)
            mark = local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if _in_blackout(local, mark, int(extra.get("aaa_blackout_minutes", 30))):
                return True
        except ValueError:
            return False
    return False


def touch_queue(snap: MarketSnapshot) -> Decimal:
    bid = snap.bids[0].qty if snap.bids else Decimal("0")
    ask = snap.asks[0].qty if snap.asks else Decimal("0")
    if bid and ask:
        return min(bid, ask)
    return bid or ask or Decimal("0")


def maker_universe_ok(
    snap: MarketSnapshot,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """True if the maker may consider this market. Risk score is still required."""
    if not filter_enabled(config) or not applies_to(snap):
        return True
    now = now or datetime.now(timezone.utc)
    allow, deny = _prefixes(config)
    extra = (config.extra.get("paper") or {}).get("series") or {}
    skip_fees = bool(extra.get("skip_maker_fee_series", True))
    if skip_fees and _has_maker_fees(snap):
        return False
    if _is_denied(snap, deny):
        return False
    if not _is_allowed(snap, allow):
        return False
    series = series_ticker(snap)
    if series.startswith("KXHIGH") and not _kxhigh_ok(snap):
        return False
    if series.startswith("KXRT") and not _kxrt_ok(snap):
        return False
    if event_blackout(snap, config, now):
        return False
    return True


def skip_reason(
    snap: MarketSnapshot,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> str | None:
    if maker_universe_ok(snap, config, now=now):
        return None
    return "series_filter"
