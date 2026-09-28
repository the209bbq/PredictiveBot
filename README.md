# PredictiveBot

Kalshi-primary paper-trading bot with Polymarket US as a second venue. The same scanner, fee, risk, and paper-trading code runs behind a shared exchange adapter.

**Production trading is disabled.** The bot never places, cancels, or modifies real orders on Kalshi production or Polymarket US, and never moves real funds. A separate opt-in path can post orders to the **Kalshi demo** environment only.

This is a personal research tool, not financial advice. Prediction-market trading is active, fee-sensitive, and legally messy; most retail users lose money.

## What it does

1. **Read-only scanner** — lists liquid Kalshi (default) or Polymarket US markets: mid, spread, book depth, volume, hours to resolution. Kalshi trading hours come from `GET /exchange/schedule`.
2. **Venue fee models**
   - **Kalshi** (fee schedule PDF + series `fee_type` on docs.kalshi.com): taker `round_up(M × 0.07 × C × P × (1−P))` to the next cent per order. Makers are free unless the series is `quadratic_with_maker_fees` (`0.0175`) or `quadratic_with_combo_maker_fees` (`0.035`).
   - **Polymarket US** (docs.polymarket.us/fees, effective 25 Sep 2026): taker `0.0695 × C × p × (1−p)`, maker rebate `0.0125 × C × p × (1−p)`, banker's rounding.
3. **Paper maker strategy** — simulated resting quotes around mid. Fills only on a strict trade-through or book cross-through. Inventory, MTM P&L, and maker cash (rebate or fee) are tracked.
4. **Paper near-resolution favorites** — resting bids on ~94–98¢ contracts near expiry. Isolated book and P&L.
5. **Risk** — per-market and gross caps, daily-loss kill switch, cancel on mid jump, resolution cutoff. All in `config.yaml`.
6. **Cross-venue comparison** — read-only match of similar events on Kalshi and Polymarket US, with the price gap **after both venues' taker fees**. Alerts only; it never trades the gap.
7. **Kalshi demo orders (opt-in)** — `pmbot kalshi-demo-order --confirm-demo` posts to `https://external-api.demo.kalshi.co/trade-api/v2` only. Production URLs are refused.

## Setup

Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Keys are **not required** for scanning, paper trading, or the comparison report. Public market data is unauthenticated on both venues.

## Commands

Kalshi scanner against **live public production data** (no keys):

```bash
python -m polymarket_bot scan --exchange kalshi --source live
```

Same scanner on Polymarket US (`polymarket-us` SDK → `https://gateway.polymarket.us`):

```bash
python -m polymarket_bot scan --exchange polymarket_us --source live
```

Fixtures (offline):

```bash
python -m polymarket_bot scan --exchange kalshi --source fixture
python -m polymarket_bot scan --exchange polymarket_us --source fixture
```

Paper session. Default venue is Kalshi; replay fixtures produce fills without waiting on the tape:

```bash
python -m polymarket_bot paper --exchange kalshi --source replay --no-sleep --ticks 3
python -m polymarket_bot paper --exchange polymarket_us --source replay --no-sleep --ticks 3
```

Live public books, still **simulated** orders only:

```bash
python -m polymarket_bot paper --exchange kalshi --source live --ticks 4 --no-sleep
```

Cross-venue comparison (alerts only):

```bash
python -m polymarket_bot compare
```

Kalshi **demo** order (requires demo keys in `.env`, `kalshi.demo_orders_enabled: true`, and `--confirm-demo`):

```bash
python -m polymarket_bot kalshi-demo-order --ticker SOME-TICKER --side bid --price 0.0100 --count 1 --confirm-demo
```

Disabled production path:

```bash
python -m polymarket_bot live
# always exits with an error
```

```bash
pytest
```

## Safety

- `dry_run: true`, `live_trading_enabled: false`.
- `POLYMARKET_LIVE_TRADING` and `KALSHI_LIVE_TRADING` must stay false.
- Kalshi demo orders require `kalshi.demo_orders_enabled: true`, `--confirm-demo`, and a demo host. `https://external-api.kalshi.com` / `https://api.elections.kalshi.com` are refused.
- Demo keys: `KALSHI_DEMO_API_KEY_ID` plus `KALSHI_DEMO_PRIVATE_KEY_PATH` or `KALSHI_DEMO_PRIVATE_KEY`. Never committed.
- Paper quotes never leave the process. Polymarket US has no order path at all.

## Data sources (verified)

### Kalshi — [docs.kalshi.com](https://docs.kalshi.com)

| Item | What we found |
| --- | --- |
| REST | Trade API v2. Recommended prod `https://external-api.kalshi.com/trade-api/v2`; demo `https://external-api.demo.kalshi.co/trade-api/v2`. |
| Public data | Markets, order books, events, series, `GET /exchange/schedule` — no auth. Confirmed 200 in this environment. |
| Order book | YES bids and NO bids only. A NO bid at `p` is a YES ask at `1−p`. |
| Auth | `KALSHI-ACCESS-KEY` / `TIMESTAMP` / `SIGNATURE`. RSA-PSS SHA-256 or Ed25519 over `timestamp + METHOD + path` (no query string). Used only for demo orders. |
| Demo orders | `POST /portfolio/events/orders` (Create Order V2). |
| Rate limits | Token buckets. Basic: 200 read / 100 write tokens per second; most calls cost 10 tokens. 429 body `{"error":"too many requests"}`, **no Retry-After**. Client uses exponential backoff. |
| Fees | Taker `round_up(0.07 × C × P × (1−P))` to the cent. Maker $0 unless the series has maker fees. |

Demo market prices may not match production. Scanner/paper default to **production public data**. Demo is for the opt-in order command (and `--source kalshi-demo-data` if you want to inspect demo books).

### Polymarket US — [docs.polymarket.us](https://docs.polymarket.us)

| Item | What we found |
| --- | --- |
| Public REST | `https://gateway.polymarket.us` — no API key. |
| Authenticated REST | `https://api.polymarket.us` — unused. |
| Official SDK | `polymarket-us` 1.0.2. |
| Fees | Taker Θ 0.0695 / maker rebate 0.0125, banker's rounding, 25 Sep 2026. |
| Rate limit | 20 r/s per IP; this bot fetches book only and paces requests. |

## Config

`config.yaml` is the single knob file: `exchange`, Kalshi URLs, scan filters, quote size, risk, comparison thresholds. Defaults are small.

## What would be needed for Kalshi or Polymarket production (not enabled)

Not implemented, not hidden behind a flag:

1. KYC'd, funded accounts on the venue you intend to trade.
2. Production API keys in env vars only (Kalshi production keys are distinct from demo keys).
3. New code that calls production order endpoints — **none of that is here**.
4. Paper results you trust, plus idempotency, reconciliation, and a real cancel-all kill switch.
5. Legal/tax review for event contracts.

Until that exists, keep `live_trading_enabled: false` and `kalshi.demo_orders_enabled` off unless you are deliberately posting to demo.
