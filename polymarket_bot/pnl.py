"""Persist and roll up P&L. Demo and live/paper histories stay separate."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_bot.logging_utils import json_default

ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def env_name(*, demo: bool, live: bool) -> str:
    if live:
        return "live"
    if demo:
        return "demo"
    return "paper"


def history_path(base: Path, env: str) -> Path:
    """Keep demo and live/paper files strictly separate."""
    if env == "demo":
        return base.with_name(base.stem + "_demo" + base.suffix)
    if env == "live":
        return base.with_name(base.stem + "_live" + base.suffix)
    return base.with_name(base.stem + "_paper" + base.suffix)


def _strategy_block(state: dict[str, Any], name: str) -> dict[str, Any]:
    block = state.get(name)
    if isinstance(block, dict) and ("equity" in block or "net_pnl" in block or "positions" in block):
        return block
    if name == "maker" and isinstance(state.get("maker"), dict):
        return state["maker"]
    return {}


def snapshot_from_state(state: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    demo = bool(state.get("demo"))
    live = bool(state.get("live"))
    env = env_name(demo=demo, live=live)
    maker = _strategy_block(state, "maker")
    near = _strategy_block(state, "near_resolution")
    if not maker and not near and (state.get("ending_cash") is not None or state.get("account_risk")):
        maker = {
            "equity": state.get("ending_cash") or state.get("starting_cash") or ZERO,
            "cash": state.get("ending_cash") or ZERO,
            "net_pnl": _dec(state.get("ending_cash")) - _dec(state.get("starting_cash")),
            "realized_pnl": ZERO,
            "rebates": ZERO,
            "positions": (state.get("maker") or {}).get("positions") or {},
            "max_drawdown": ZERO,
            "fill_count": state.get("fill_count") or 0,
        }
    strategies = {}
    markets: dict[str, dict[str, Any]] = {}
    total_equity = ZERO
    total_cash = ZERO
    total_realized = ZERO
    total_unrealized = ZERO
    total_fees = ZERO
    total_pnl = ZERO
    max_dd = ZERO
    wins = losses = 0
    for name, block in (("maker", maker), ("near_resolution", near)):
        if not block:
            continue
        equity = _dec(block.get("equity"))
        cash = _dec(block.get("cash"))
        realized = _dec(block.get("realized_pnl"))
        fees = _dec(block.get("rebates"))
        net = _dec(block.get("net_pnl"))
        unrealized = net - realized
        strategies[name] = {
            "equity": equity,
            "cash": cash,
            "realized": realized,
            "unrealized": unrealized,
            "fees": fees,
            "net_pnl": net,
            "max_drawdown": _dec(block.get("max_drawdown")),
            "fill_count": int(block.get("fill_count") or 0),
        }
        total_equity += equity
        total_cash += cash
        total_realized += realized
        total_unrealized += unrealized
        total_fees += fees
        total_pnl += net
        max_dd = max(max_dd, _dec(block.get("max_drawdown")))
        for slug, pos in (block.get("positions") or {}).items():
            qty = _dec(pos.get("qty"))
            avg = _dec(pos.get("avg_price"))
            r = _dec(pos.get("realized_pnl"))
            row = markets.setdefault(
                slug,
                {"qty": ZERO, "avg_price": avg, "realized": ZERO, "unrealized": ZERO, "strategy": name},
            )
            row["qty"] += qty
            row["realized"] += r
            row["avg_price"] = avg
            row["strategy"] = name
            if r > 0:
                wins += 1
            elif r < 0:
                losses += 1
    return {
        "ts": now.isoformat(),
        "env": env,
        "equity": total_equity,
        "cash": total_cash,
        "realized": total_realized,
        "unrealized": total_unrealized,
        "fees": total_fees,
        "net_pnl": total_pnl,
        "max_drawdown": max_dd,
        "wins": wins,
        "losses": losses,
        "strategies": strategies,
        "markets": markets,
    }


def append_pnl(base_path: Path, state: dict[str, Any], *, now: datetime | None = None) -> Path:
    snap = snapshot_from_state(state, now=now)
    path = history_path(base_path, str(snap["env"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snap, default=json_default) + "\n")
    return path


def load_history(base_path: Path, env: str) -> list[dict[str, Any]]:
    path = history_path(base_path, env)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _in_window(ts: str, start: datetime) -> bool:
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when >= start


def summarize(rows: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)

    def roll(window: str) -> dict[str, Any]:
        if window == "today":
            picked = [r for r in rows if _in_window(str(r.get("ts") or ""), day_start)]
        elif window == "7d":
            picked = [r for r in rows if _in_window(str(r.get("ts") or ""), week_start)]
        else:
            picked = list(rows)
        if not picked:
            return {
                "net_pnl": ZERO,
                "realized": ZERO,
                "unrealized": ZERO,
                "fees": ZERO,
                "max_drawdown": ZERO,
                "wins": 0,
                "losses": 0,
                "points": 0,
            }
        last = picked[-1]
        return {
            "net_pnl": _dec(last.get("net_pnl")),
            "realized": _dec(last.get("realized")),
            "unrealized": _dec(last.get("unrealized")),
            "fees": _dec(last.get("fees")),
            "max_drawdown": max((_dec(r.get("max_drawdown")) for r in picked), default=ZERO),
            "wins": int(last.get("wins") or 0),
            "losses": int(last.get("losses") or 0),
            "points": len(picked),
        }

    equity_points = [{"ts": r.get("ts"), "equity": _dec(r.get("equity"))} for r in rows[-120:]]
    last = rows[-1] if rows else {}
    return {
        "today": roll("today"),
        "last_7d": roll("7d"),
        "all_time": roll("all"),
        "equity_points": equity_points,
        "strategies": last.get("strategies") or {},
        "markets": last.get("markets") or {},
        "latest": last,
    }
