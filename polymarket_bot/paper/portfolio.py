"""Paper book, inventory, and P&L for one isolated strategy."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from polymarket_bot.fees import maker_cashflow

ZERO = Decimal("0")


@dataclass
class PaperOrder:
    order_id: str
    market: str
    side: str  # buy | sell
    price: Decimal
    qty: Decimal
    strategy: str
    venue: str = "polymarket_us"
    fee_type: str | None = None
    fee_multiplier: Decimal = Decimal("1")


@dataclass
class Fill:
    order_id: str
    market: str
    side: str
    price: Decimal
    qty: Decimal
    rebate: Decimal
    strategy: str
    reason: str


@dataclass
class Position:
    qty: Decimal = ZERO  # signed: +long / -short
    avg_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    rebates: Decimal = ZERO
    fills: int = 0

    def apply_fill(self, side: str, price: Decimal, qty: Decimal, rebate: Decimal) -> None:
        signed = qty if side == "buy" else -qty
        self.rebates += rebate
        self.fills += 1
        if self.qty == 0 or (self.qty > 0 and signed > 0) or (self.qty < 0 and signed < 0):
            new_qty = self.qty + signed
            if new_qty == 0:
                self.qty = ZERO
                self.avg_price = ZERO
                return
            abs_old = abs(self.qty)
            abs_new = abs(signed)
            self.avg_price = ((abs_old * self.avg_price) + (abs_new * price)) / (abs_old + abs_new)
            self.qty = new_qty
            return
        # reducing or flipping
        reduce_qty = min(abs(self.qty), qty)
        direction = Decimal("1") if self.qty > 0 else Decimal("-1")
        # long reduced by sell: pnl = (sell - avg) * qty; short reduced by buy: pnl = (avg - buy) * qty
        if self.qty > 0:
            self.realized_pnl += (price - self.avg_price) * reduce_qty
        else:
            self.realized_pnl += (self.avg_price - price) * reduce_qty
        remaining = self.qty + signed
        if remaining == 0:
            self.qty = ZERO
            self.avg_price = ZERO
        elif (self.qty > 0 and remaining < 0) or (self.qty < 0 and remaining > 0):
            self.qty = remaining
            self.avg_price = price
        else:
            self.qty = remaining


@dataclass
class Portfolio:
    name: str
    cash: Decimal
    starting_cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[Decimal] = field(default_factory=list)
    peak_equity: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    killed: bool = False
    kill_reason: str | None = None

    def position(self, market: str) -> Position:
        if market not in self.positions:
            self.positions[market] = Position()
        return self.positions[market]

    def gross_position(self) -> Decimal:
        return sum((abs(p.qty) for p in self.positions.values()), ZERO)

    def mark_to_market(self, mids: dict[str, Decimal | None]) -> Decimal:
        mtm = ZERO
        for slug, pos in self.positions.items():
            mid = mids.get(slug)
            if mid is None or pos.qty == 0:
                continue
            mtm += pos.qty * mid
        return mtm

    def equity(self, mids: dict[str, Decimal | None]) -> Decimal:
        return self.cash + self.mark_to_market(mids)

    def record_equity(self, mids: dict[str, Decimal | None]) -> Decimal:
        eq = self.equity(mids)
        self.equity_curve.append(eq)
        if not self.peak_equity or eq > self.peak_equity:
            self.peak_equity = eq
        dd = self.peak_equity - eq
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        return eq

    def apply_fill(self, order: PaperOrder, qty: Decimal, reason: str) -> Fill:
        rebate = maker_cashflow(
            qty,
            order.price,
            venue=order.venue,
            fee_type=order.fee_type,
            multiplier=order.fee_multiplier,
        )
        pos = self.position(order.market)
        if order.side == "buy":
            self.cash -= order.price * qty
        else:
            self.cash += order.price * qty
        self.cash += rebate
        pos.apply_fill(order.side, order.price, qty, rebate)
        fill = Fill(
            order_id=order.order_id,
            market=order.market,
            side=order.side,
            price=order.price,
            qty=qty,
            rebate=rebate,
            strategy=order.strategy,
            reason=reason,
        )
        self.fills.append(fill)
        return fill

    def buying_power_ok(self, side: str, price: Decimal, qty: Decimal) -> bool:
        if side == "buy":
            return self.cash >= price * qty
        # Conservative short collateral: (1 - price) * qty
        return self.cash >= (Decimal("1") - price) * qty

    def to_dict(self, mids: dict[str, Decimal | None] | None = None) -> dict[str, Any]:
        mids = mids or {}
        eq = self.equity(mids)
        return {
            "name": self.name,
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "equity": eq,
            "net_pnl": eq - self.starting_cash,
            "realized_pnl": sum((p.realized_pnl for p in self.positions.values()), ZERO),
            "rebates": sum((p.rebates for p in self.positions.values()), ZERO),
            "fill_count": len(self.fills),
            "max_drawdown": self.max_drawdown,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "gross_position": self.gross_position(),
            "positions": {
                slug: {
                    "qty": p.qty,
                    "avg_price": p.avg_price,
                    "realized_pnl": p.realized_pnl,
                    "rebates": p.rebates,
                    "fills": p.fills,
                }
                for slug, p in self.positions.items()
                if p.qty != 0 or p.fills
            },
        }
