"""PT-day loss stop and dollar capital-in-use cap.

Enforced in every environment (paper, demo, live). Winning is not capped.
The loss day is America/Los_Angeles midnight to midnight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from polymarket_bot.config import AppConfig

PT = ZoneInfo("America/Los_Angeles")
ZERO = Decimal("0")


@dataclass
class DailyLimits:
    pt_date: str
    start_equity: Decimal
    equity: Decimal
    day_pnl: Decimal
    loss_limit: Decimal
    loss_halted: bool
    just_triggered: bool
    capital_in_use: Decimal
    capital_limit: Decimal

    @property
    def capital_blocked(self) -> bool:
        return self.capital_in_use > self.capital_limit

    def as_dict(self) -> dict[str, Any]:
        return {
            "pt_date": self.pt_date,
            "start_equity": self.start_equity,
            "equity": self.equity,
            "daily_pnl_usd": self.day_pnl,
            "max_daily_loss_usd": self.loss_limit,
            "daily_loss_limit_hit": self.loss_halted,
            "daily_capital_in_use_usd": self.capital_in_use,
            "max_daily_capital_in_use_usd": self.capital_limit,
        }


def pt_date(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(PT).date().isoformat()


def daily_limits_path(config: AppConfig) -> Path:
    env = getattr(config, "environment", None) or "paper"
    return Path(config.trading.toggle_path).parent / f"daily_limits_{env}.json"


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_store(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def empty_limits(config: AppConfig, *, now: datetime | None = None) -> DailyLimits:
    now = now or datetime.now(timezone.utc)
    today = pt_date(now)
    risk = config.paper.risk
    store = load_store(daily_limits_path(config))
    # Halt is PT-day scoped. Status/dashboard must resume after midnight even
    # if the trading loop has not written a new store yet.
    if store.get("pt_date") != today:
        return DailyLimits(
            pt_date=today,
            start_equity=ZERO,
            equity=ZERO,
            day_pnl=ZERO,
            loss_limit=risk.max_daily_loss_usd,
            loss_halted=False,
            just_triggered=False,
            capital_in_use=_dec(store.get("capital_in_use"), "0"),
            capital_limit=risk.max_daily_capital_in_use_usd,
        )
    start = _dec(store.get("start_equity"), "0")
    equity = _dec(store.get("equity"), str(start))
    return DailyLimits(
        pt_date=today,
        start_equity=start,
        equity=equity,
        day_pnl=equity - start if start else ZERO,
        loss_limit=risk.max_daily_loss_usd,
        loss_halted=bool(store.get("loss_halted")),
        just_triggered=False,
        capital_in_use=_dec(store.get("capital_in_use"), "0"),
        capital_limit=risk.max_daily_capital_in_use_usd,
    )


def refresh_daily_limits(
    config: AppConfig,
    *,
    equity: Decimal,
    capital_in_use: Decimal,
    now: datetime | None = None,
    persist: bool = True,
) -> DailyLimits:
    now = now or datetime.now(timezone.utc)
    path = daily_limits_path(config)
    store = load_store(path)
    today = pt_date(now)
    if store.get("pt_date") != today:
        store = {
            "pt_date": today,
            "start_equity": str(equity),
            "loss_halted": False,
        }
    start = _dec(store.get("start_equity"), str(equity))
    day_pnl = equity - start
    limit = config.paper.risk.max_daily_loss_usd
    was = bool(store.get("loss_halted"))
    halted = was or day_pnl <= -limit
    store["loss_halted"] = halted
    store["equity"] = str(equity)
    store["capital_in_use"] = str(capital_in_use)
    store["updated_at"] = now.isoformat()
    if persist:
        save_store(path, store)
    return DailyLimits(
        pt_date=today,
        start_equity=start,
        equity=equity,
        day_pnl=day_pnl,
        loss_limit=limit,
        loss_halted=halted,
        just_triggered=halted and not was,
        capital_in_use=capital_in_use,
        capital_limit=config.paper.risk.max_daily_capital_in_use_usd,
    )
