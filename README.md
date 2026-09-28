# PredictiveBot

Dry-run Polymarket US market scanner and paper-trading bot. This version **never places, cancels, or modifies real orders** and **never moves funds**. There is no live-trading implementation behind a hidden switch: if you turn live mode on, the process raises.

This is a personal research tool, not financial advice. Prediction-market trading is active, fee-sensitive, and legally messy; most retail users lose money.

## What it does

1. **Read-only scanner** — lists active Polymarket US markets with mid, spread, book depth, volume (`sharesTraded`), and hours to resolution, then keeps the liquid ones.
2. **Fee model** — Sep 25, 2026 Polymarket US schedule from [docs.polymarket.us/fees](https://docs.polymarket.us/fees):
   - Taker: `0.0695 × C × p × (1 − p)` (per-market `feeCoefficient` when present)
   - Maker rebate: `0.0125 × C × p × (1 − p)`
   - Combo taker curve is implemented and tested, but the paper strategies are maker-only.
   - Banker's rounding to the nearest cent.
3. **Paper maker strategy** — simulated resting bid/ask around mid on a handful of liquid markets. Fills only when the market **trades through** or the book **crosses through** the quote (prints *at* the quote do not fill). Rebates are credited. Inventory and mark-to-market P&L are tracked.
4. **Paper near-resolution favorites** — optional second book that posts resting bids on ~94–98¢ contracts close to resolution. Reported separately so you can compare it to the maker book.
5. **Risk** — per-market and gross position caps, max daily loss kill switch, cancel-all on large mid jumps, time-to-resolution cutoff. All knobs live in `config.yaml` with small, conservative defaults.
6. **Logs + report** — every simulated decision is appended as JSONL; the CLI prints paper P&L net of fees/rebates, fill counts, and max drawdown.

## Setup

Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Keys are **not required** for scanning or paper trading. Public market data is unauthenticated.

## Commands

Scanner against **live public data** (official `polymarket-us` SDK → `https://gateway.polymarket.us`):

```bash
python -m polymarket_bot scan --source live
```

Scanner against the recorded snapshot in `fixtures/public_markets.json`:

```bash
python -m polymarket_bot scan --source fixture
```

Short paper session with a **replay fixture** (produces fills and a report without waiting on the tape):

```bash
python -m polymarket_bot paper --source replay --no-sleep --ticks 3
```

Paper session that **polls live public books** (still simulated orders only; fills are rare in a few ticks because the fill model is conservative):

```bash
python -m polymarket_bot paper --source live --ticks 4 --no-sleep
```

Reprint the last report:

```bash
python -m polymarket_bot report
```

The disabled live path:

```bash
python -m polymarket_bot live
# always exits with an error; never sends an order
```

Tests:

```bash
pytest
```

## Dry-run guarantee

- `dry_run: true` and `live_trading_enabled: false` in `config.yaml`.
- `POLYMARKET_LIVE_TRADING` in `.env` must stay false. `true` / `1` / `yes` makes startup raise.
- The SDK is constructed **without** keys. `orders`, `account`, `portfolio`, and `ws` are replaced with stubs that raise.
- Paper "quotes" and "cancels" are in-memory only.
- `python -m polymarket_bot live` raises `LiveTradingDisabled`.

## Data source and API notes

Verified against [docs.polymarket.us](https://docs.polymarket.us) (retail API, `polymarket-us` 1.0.2, fees page, WebSockets):

| Item | What we found |
| --- | --- |
| Public REST | `https://gateway.polymarket.us` — no API key. This is what the scanner uses. |
| Authenticated REST | `https://api.polymarket.us` — Key ID + Ed25519 signature. Trading, portfolio, balances. Unused here. |
| `GET https://api.polymarket.us/v1/markets` without keys | 401 `Missing required API key headers` (and some bot User-Agents are blocked). Do not confuse this host with the public gateway. |
| Official SDK | `pip install polymarket-us`. `PolymarketUS()` with no keys talks to the gateway for `markets.list` / `book` / `bbo`. Confirmed working. |
| List payload | Includes `bestBidQuote` / `bestAskQuote`, `endDate`, `feeCoefficient`, `status`. **`volume` and `liquidity` are often null** on the list endpoint. Volume and depth come from `/v1/markets/{slug}/bbo` (`sharesTraded`, `bidShares`, `askShares`) and `/book`. |
| WebSockets | `wss://api.polymarket.us/v1/ws/markets` **requires API key authentication**. Not used in this dry-run version; paper trading polls REST (or a fixture replay). |
| Rate limit | 20 r/s per IP on public endpoints. Config paces requests (`min_request_interval_seconds`). |
| Fees | Standard Θ taker 0.0695 / maker rebate 0.0125, banker's rounding, effective Sep 25, 2026. Upcoming table-tennis taker Θ `0.10` on Sep 30, 2026; per-market `feeCoefficient` is read when present. |

Recorded fixtures in `fixtures/` exist so tests and demos do not depend on the network.

## Config

Everything is in `config.yaml`: scan filters, quote size, half-spread, position caps, daily-loss kill, jump cancel, resolution cutoff, tick count. Defaults are intentionally small.

## What would be needed to go live (not enabled)

This repo will not do it for you. A future version would still need all of the following, plus a lot of extra engineering:

1. A Polymarket US account in the app, **full KYC**, and eligibility in your state ([signup docs](https://docs.polymarket.us/learn/get-started/signup)).
2. Funded account (ACH / debit / wire).
3. API keys from [polymarket.us/developer](https://polymarket.us/developer), stored only in environment variables (`POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY`).
4. New code that calls `client.orders.create` / `cancel` / `modify` — **none of that is implemented here**.
5. Authenticated market-data WebSocket instead of REST polling.
6. Months of paper results you actually trust, plus operational pieces: idempotent orders, reconciliation against positions, alerting, and a kill switch that hits the real cancel-all endpoint.
7. Legal/tax review for event contracts. New York has sued Polymarket US; state availability changes.

Until that exists, keep `live_trading_enabled: false`.
