"""Text report comparing maker vs near-resolution paper results."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _money(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"${value.quantize(Decimal('0.01'))}"
    try:
        return f"${Decimal(str(value)).quantize(Decimal('0.01'))}"
    except Exception:
        return str(value)


def format_strategy(block: dict[str, Any]) -> str:
    lines = [
        f"  Net P&L (mark-to-market, net of maker rebates): {_money(block.get('net_pnl'))}",
        f"  Realized P&L: {_money(block.get('realized_pnl'))}",
        f"  Maker fees/rebates (signed): {_money(block.get('rebates'))}",
        f"  Fills: {block.get('fill_count', 0)}",
        f"  Max drawdown: {_money(block.get('max_drawdown'))}",
        f"  Ending equity: {_money(block.get('equity'))}   cash {_money(block.get('cash'))}",
        f"  Gross inventory (contracts): {block.get('gross_position')}",
    ]
    if block.get("killed"):
        lines.append(f"  Kill switch: {block.get('kill_reason')}")
    positions = block.get("positions") or {}
    if positions:
        lines.append("  Positions:")
        for slug, pos in positions.items():
            lines.append(
                f"    {slug}: qty={pos['qty']} avg={pos['avg_price']} "
                f"realized={pos['realized_pnl']} rebates={pos['rebates']}"
            )
    else:
        lines.append("  Positions: (flat)")
    return "\n".join(lines)


def format_report(state: dict[str, Any]) -> str:
    maker = state.get("maker") or {}
    near = state.get("near_resolution") or {}
    combined_pnl = Decimal(str(maker.get("net_pnl") or 0)) + Decimal(str(near.get("net_pnl") or 0))
    combined_fills = int(maker.get("fill_count") or 0) + int(near.get("fill_count") or 0)
    dd_m = Decimal(str(maker.get("max_drawdown") or 0))
    dd_n = Decimal(str(near.get("max_drawdown") or 0))
    lines = [
        "Paper-trading report",
        "DRY-RUN ONLY — no production orders, cancels, or fund movements",
        "",
        f"Data source: {state.get('source')}",
        f"Venue: {state.get('venue') or 'n/a'}",
        f"Ticks: {state.get('ticks')}    Markets: {', '.join(state.get('markets') or []) or '(none)'}",
        "",
        "Maker-only quotes around mid",
        format_strategy(maker),
        "",
        "Near-resolution favorites (resting bids ~94–98¢)",
        format_strategy(near),
        "",
        "Comparison",
        f"  Combined net P&L: {_money(combined_pnl)}",
        f"  Combined fills: {combined_fills}",
        f"  Worse max drawdown of the two books: {_money(max(dd_m, dd_n))}",
        "",
        "Fills are simulated conservatively (trade-through or book-cross only).",
        "Kalshi maker cash is usually $0 (or a maker fee); Polymarket US credits a rebate.",
    ]
    return "\n".join(lines) + "\n"
