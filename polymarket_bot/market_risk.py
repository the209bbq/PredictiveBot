"""Per-market RISK SCORE in [0, 1]. 1.0 is an extremely volatile / live market.

The bot only quotes markets strictly below `max_market_risk_score`
(default 0.40, hard maximum 0.50 unless explicitly overridden).

Components (each 0–1), then a weighted sum. A live in-game sports market (including player props on a started
or same-day game) floors the score at 1.00 so it can never be quoted.

    volatility  range of recent mids / 0.20
    jump        largest recent |Δmid| or |last−mid| / 0.15
    spread      quoted spread / 0.10
    thin_book   1 − min(1, (bid+ask depth) / 200)
    urgency     1 at ≤1h to event, 0 at ≥48h
    coin_flip   closeness to 50/50, stronger on news-driven events
    live_game   1 if an in-game / same-day GAME market

score = min(1, 0.20·vol + 0.18·jump + 0.12·spread + 0.10·thin
              + 0.15·urgency + 0.15·coin)
if live_game: score = 1.00
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from polymarket_bot.market_data import MarketSnapshot, as_datetime

ZERO = Decimal("0")
ONE = Decimal("1")
HARD_MAX_MARKET_RISK_SCORE = Decimal("0.50")
DEFAULT_MARKET_RISK_SCORE = Decimal("0.40")

_NEWS = (
    "news", "headline", "resign", "indict", "election", "fed ", "rate cut",
    "ceo", "merger", "lawsuit", "arrest", "impeach",
)
_LIVE_PHRASES = (
    "in progress", "in-game", "in game", "live game", "halftime",
    "1st quarter", "2nd quarter", "3rd quarter", "4th quarter",
    "top of the", "bottom of the", "end of the",
)
_GAME_TICKER = re.compile(r"GAME", re.IGNORECASE)
_DATE_TEAM = re.compile(r"\d{2}[A-Z]{3}\d{2}[A-Z]{4,}")
_DATE_TOKEN = re.compile(r"(\d{2})([A-Z]{3})(\d{2})(?=[A-Z]|[-_]|$)")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_SPORTS = ("nfl", "nba", "mlb", "nhl", "ncaa", "wnba", "soccer", "epl", "football", "basketball")
_PROP = (
    "receiving", "rushing", "passing yards", "receiving yards", "rushing yards",
    "touchdown", "anytime", "strikeout", "receptions", "completions",
    "player prop", " yards", "rebounds", "assists +",
)


class MarketRiskError(ValueError):
    """Invalid max_market_risk_score configuration."""


def validate_market_risk_score(score: Decimal, *, allow_above_hard_max: bool) -> Decimal:
    if score < ZERO or score > ONE:
        raise MarketRiskError("max_market_risk_score must be between 0 and 1 inclusive.")
    if score > HARD_MAX_MARKET_RISK_SCORE and not allow_above_hard_max:
        raise MarketRiskError(
            f"max_market_risk_score {score} exceeds the hard maximum "
            f"{HARD_MAX_MARKET_RISK_SCORE}. Set allow_market_risk_above_hard_max: true "
            "to override."
        )
    return score


def _clamp01(value: Decimal) -> Decimal:
    if value < ZERO:
        return ZERO
    if value > ONE:
        return ONE
    return value


def _market_ids(snap: MarketSnapshot) -> str:
    market = (snap.raw or {}).get("market") or {}
    return " ".join(
        filter(
            None,
            [
                snap.slug,
                str(market.get("ticker") or ""),
                str(market.get("series_ticker") or ""),
                str(market.get("event_ticker") or ""),
            ],
        )
    )


def _blob(snap: MarketSnapshot) -> str:
    market = (snap.raw or {}).get("market") or {}
    return " ".join(
        filter(
            None,
            [
                snap.slug,
                snap.question,
                snap.event_title,
                snap.category,
                str(snap.status or ""),
                str(market.get("series_ticker") or ""),
                str(market.get("event_ticker") or ""),
                str(market.get("yes_sub_title") or ""),
                str(market.get("subtitle") or ""),
            ],
        )
    ).lower()


def _dates_in_ids(snap: MarketSnapshot) -> list:
    ids = f"{_market_ids(snap)} {_blob(snap)}".upper()
    out = []
    for match in _DATE_TOKEN.finditer(ids):
        year, mon, day = match.group(1), match.group(2), match.group(3)
        month = _MONTHS.get(mon)
        if month is None:
            continue
        try:
            parsed = datetime(2000 + int(year), month, int(day), tzinfo=timezone.utc).date()
        except ValueError:
            continue
        if parsed not in out:
            out.append(parsed)
    return out


def game_date_is_active(snap: MarketSnapshot, now: datetime) -> bool:
    """Ticker/event dates like 26SEP28: live on that calendar day and the next."""
    today = now.date()
    for game_day in _dates_in_ids(snap):
        if timedelta(0) <= (today - game_day) <= timedelta(days=1):
            return True
    return False


def looks_like_player_prop(snap: MarketSnapshot) -> bool:
    text = _blob(snap)
    return any(tok in text for tok in _PROP)


def looks_like_game_market(snap: MarketSnapshot) -> bool:
    ids = _market_ids(snap)
    if _GAME_TICKER.search(ids) or _DATE_TEAM.search(ids):
        return True
    text = _blob(snap)
    if any(tok in text for tok in (" vs ", " vs. ", " @ ")) and any(s in text for s in _SPORTS):
        return True
    if looks_like_player_prop(snap) and (
        any(s in text for s in _SPORTS) or _GAME_TICKER.search(ids) or _DATE_TEAM.search(ids)
    ):
        return True
    return False


def is_live_in_game(snap: MarketSnapshot, *, now: datetime | None = None) -> bool:
    """True for in-progress games and player props on a started / same-day game.

    Those markets score 100% and are never quoted. Kickoff uses occurrence
    time when present; settlement close is ignored so live props cannot hide
    behind a late official close.
    """
    text = _blob(snap)
    if any(p in text for p in _LIVE_PHRASES):
        return True
    game_linked = looks_like_game_market(snap) or looks_like_player_prop(snap)
    if not game_linked:
        return False
    hours = snap.hours_to_resolution
    market = (snap.raw or {}).get("market") or {}
    occ = as_datetime(
        market.get("occurrence_datetime") or market.get("event_occurrence_datetime")
    )
    now = now or datetime.now(timezone.utc)
    if occ is not None:
        return occ <= now
    # No kickoff on the market (common on player props). A date in the
    # ticker/event/series (26SEP28) marks a same-day game even when official
    # close/expiration is days later — the 6h cutoff never trips.
    if game_date_is_active(snap, now):
        return True
    if hours is None or hours < 24:
        return True
    return False


def is_news_driven(snap: MarketSnapshot) -> bool:
    text = _blob(snap)
    return any(tok in text for tok in _NEWS)


@dataclass
class PriceHistory:
    """Recent mids used for volatility / jump. One deque per market."""

    window: int = 12
    _mids: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    def observe(self, slug: str, mid: Decimal | None) -> None:
        if mid is None or not slug:
            return
        series = self._mids[slug]
        if series.maxlen != self.window:
            self._mids[slug] = series = deque(series, maxlen=self.window)
        series.append(Decimal(mid))

    def price_range(self, slug: str) -> Decimal:
        xs = self._mids.get(slug)
        if not xs or len(xs) < 2:
            return ZERO
        return max(xs) - min(xs)

    def largest_step(self, slug: str) -> Decimal:
        xs = list(self._mids.get(slug) or [])
        if len(xs) < 2:
            return ZERO
        return max(abs(xs[i] - xs[i - 1]) for i in range(1, len(xs)))


def _components(
    snap: MarketSnapshot,
    history: PriceHistory | None,
    now: datetime | None = None,
) -> dict[str, Decimal]:
    mid = snap.mid
    spread = snap.spread if snap.spread is not None else ZERO
    depth = (snap.bid_depth_contracts or ZERO) + (snap.ask_depth_contracts or ZERO)
    hours = snap.hours_to_resolution

    vol = ZERO
    jump = ZERO
    if history is not None:
        vol = _clamp01(history.price_range(snap.slug) / Decimal("0.20"))
        jump = _clamp01(history.largest_step(snap.slug) / Decimal("0.15"))
    if snap.last_trade is not None and mid is not None:
        jump = max(jump, _clamp01(abs(snap.last_trade - mid) / Decimal("0.15")))

    spread_c = _clamp01(spread / Decimal("0.10"))
    touch = Decimal("0")
    if snap.bids and snap.asks:
        touch = min(snap.bids[0].qty, snap.asks[0].qty)
    thin = _clamp01(ONE - min(ONE, (depth + touch * 2) / Decimal("200")))
    if hours is None:
        urgency = Decimal("0.30")
    elif hours <= 1:
        urgency = ONE
    elif hours >= 48:
        urgency = ZERO
    else:
        urgency = _clamp01((Decimal("48") - Decimal(str(hours))) / Decimal("47"))

    if mid is None:
        coin = Decimal("0.50")
    else:
        proximity = _clamp01(ONE - abs(mid - Decimal("0.50")) * 2)
        scale = ONE if is_news_driven(snap) else Decimal("0.35")
        coin = _clamp01(proximity * scale)

    live = ONE if is_live_in_game(snap, now=now) else ZERO
    return {
        "volatility": vol,
        "jump": jump,
        "spread": spread_c,
        "thin_book": thin,
        "urgency": urgency,
        "coin_flip": coin,
        "live_game": live,
    }


def compute_market_risk(
    snap: MarketSnapshot,
    history: PriceHistory | None = None,
    *,
    now: datetime | None = None,
) -> tuple[Decimal, dict[str, Decimal]]:
    parts = _components(snap, history, now=now)
    score = (
        Decimal("0.20") * parts["volatility"]
        + Decimal("0.18") * parts["jump"]
        + Decimal("0.12") * parts["spread"]
        + Decimal("0.10") * parts["thin_book"]
        + Decimal("0.15") * parts["urgency"]
        + Decimal("0.15") * parts["coin_flip"]
    )
    score = _clamp01(score)
    if parts["live_game"] >= ONE:
        score = ONE
    return score, parts


def attach_market_risk(
    snap: MarketSnapshot,
    history: PriceHistory | None = None,
    *,
    now: datetime | None = None,
) -> MarketSnapshot:
    if history is not None:
        history.observe(snap.slug, snap.mid)
    score, parts = compute_market_risk(snap, history, now=now)
    snap.risk_score = score
    snap.risk_components = {k: v for k, v in parts.items()}
    return snap


def format_score(score: Decimal | None) -> str:
    if score is None:
        return "-"
    return f"{(score * Decimal('100')).quantize(Decimal('0.0'))}%"


def market_over_risk_threshold(snap: MarketSnapshot, max_score: Decimal) -> bool:
    if snap.risk_score is None:
        attach_market_risk(snap)
    return (snap.risk_score or ZERO) >= max_score


def score_payload(snap: MarketSnapshot) -> dict[str, Any]:
    return {
        "score": snap.risk_score,
        "score_display": format_score(snap.risk_score),
        "components": dict(snap.risk_components or {}),
    }
