# CURSOR_CONTEXT — Aster Funding Rate Farmer Suite

## Project overview
Automated funding rate farming suite for Aster DEX perpetuals.
Two bots in one repo — use `funding_farmer.py` for the main strategy;
optional Hyperliquid hedge leg lives in `delta_neutral.py` (enabled with `DELTA_NEUTRAL=true` in `.env`).

---

## File map

| File | Purpose |
|---|---|
| `funding_farmer.py` | Main bot — Aster-only, multi-symbol diversified funding farm |
| `aster_client.py` | Aster REST — Pro API V3 (EIP-712) or legacy HMAC |
| `delta_neutral.py` | Optional extension — Aster LONG + HL SHORT hedge leg |
| `.env.example` | All config options with comments, copy to `.env` |
| `requirements.txt` | pip deps |
| `trades.csv` | Auto-created on first trade, every open/close logged here |
| `funding_farmer.log` | Colour-coded runtime log (includes HL lines when `DELTA_NEUTRAL=true`) |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in Pro API V3 (ASTER_USER, ASTER_SIGNER, ASTER_SIGNER_PRIVATE_KEY)
python funding_farmer.py
```

**API keys:** Use **Pro API V3** (recommended). Register an **AGENT** at [asterdex.com API Wallet](https://www.asterdex.com/en/api-wallet) (switch to **Pro API** at the top). See [Aster API docs](https://github.com/asterdex/api-docs/blob/master/README.md) — **V3 Futures** is the current integration. Legacy HMAC (`ASTER_API_KEY` + `ASTER_SECRET_KEY`) is still supported if you have old keys.

**Margin assets to deposit before running:**
- USDF (99.99% collateral ratio) — mint via Aster Earn or buy on Aster Spot
- ASTER tokens (80% collateral ratio) — just deposit, they work as margin automatically
Both earn Stage 6 Aster Asset Points as a free bonus on top of trading/position points.

---

## funding_farmer.py — architecture

```
run()
├── enable_multi_asset_mode()        POST /fapi/v3/multiAssetsMargin (V3 Pro)
├── get_collateral_summary()         GET  /fapi/v3/balance  (ASTER+USDF+USDT)
├── get_exchange_info()              GET  /fapi/v3/exchangeInfo (step sizes)
│
└── while True:
    ├── get_all_funding_rates()      GET  /fapi/v1/premiumIndex (all symbols)
    ├── get_collateral_summary()     live wallet read every cycle
    ├── compute_deploy_budget()      wallet * WALLET_DEPLOY_PCT
    ├── available_budget()           total_budget - already deployed
    │
    ├── check_stop_loss()            compare entryPrice vs markPrice
    ├── close_long(..., "stop_loss")
    │
    ├── funding flip exit            rate < EXIT_FUNDING_RATE -> close (REST lastFundingRate; optional WS ``r``)
    ├── close_long(..., "funding_dropped")
    │
    ├── [build candidates]           incremental pending set for corr guard
    ├── rank_weighted_sizes()        #1 gets RANK_TOP_PCT, rest equal split
    ├── is_correlated()              blocks correlated pairs (BTC+WBTC etc)
    │
    ├── open_long(sym, notional)     POST /fapi/v3/order MARKET BUY
    │   └── log_trade_open()         → trades.csv OPEN row
    │
    └── close_long(sym, reason)      POST /fapi/v3/order MARKET SELL reduceOnly
        └── log_trade_close()        → trades.csv CLOSE row with PnL
```

---

## Key API endpoints

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| GET | `/fapi/v1/premiumIndex` | None | All symbols: `lastFundingRate`, `nextFundingTime`, `markPrice` |
| GET | `/fapi/v1/exchangeInfo` | None | Symbol filters (stepSize, minQty) |
| GET | `/fapi/v3/balance` | Signed | Account balances per asset |
| GET | `/fapi/v3/positionRisk` | Signed | Open positions |
| POST | `/fapi/v3/multiAssetsMargin` | Signed | Enable multi-asset margin mode |
| POST | `/fapi/v3/leverage` | Signed | Set leverage per symbol |
| POST | `/fapi/v3/marginType` | Signed | Set CROSSED margin |
| POST | `/fapi/v3/order` | Signed | Place market order |

**Auth (Pro API V3):** EIP-712 `AsterSignTransaction` on the urlencoded parameter string; each request includes `user` (main wallet), `signer` (API agent), `nonce` (microseconds), and `signature`. Base URL: `https://fapi.asterdex.com` (override with `ASTER_FAPI_BASE`). See [Futures API V3 (EN)](https://github.com/asterdex/api-docs/blob/master/V3(Recommended)/EN/aster-finance-futures-api-v3.md).

**Auth (legacy):** HMAC-SHA256 + `X-MBX-APIKEY`; paths stay `/fapi/v1` and `/fapi/v2/`.

---

## Dry run mode

`DRY_RUN=true` in `.env` — the default in `.env.example`. Set to `false` when ready to go live.

**Sizing vs simulation:** With **`DRY_RUN_SIMULATED_MARGIN_USD=0`** (default in `.env.example`), the deploy budget uses your **real** effective futures margin from the API — same formula as live — while **orders stay simulated**. With **`DRY_RUN_SIMULATED_MARGIN_USD > 0`**, sizing uses that fixed USD instead of your wallet (useful for fixed “what if $2k” runs or quieter dependency on balance).

**What runs live in dry run:**
- Rate scanning (`GET /fapi/v1/premiumIndex`) — real Aster data
- Wallet/balance fetch (`GET /fapi/v2/balance`, signed) — real balances for display and (when simulated margin is 0) for sizing
- Price fetches (`GET /fapi/v1/premiumIndex` per symbol) — real prices

**What is simulated:**
- `enable_multi_asset_mode` → skipped (no API call)
- `set_leverage` / `set_cross_margin` → logged only
- `open_long` → stored in `_dry_positions` dict, fake orderId `DRY_BTCUSDT_1`
- `close_long` → reads from `_dry_positions`, calls `get_mark_price` for live exit price
- `check_stop_loss` → checks `_dry_positions` instead of live `get_positions()`

**trades.csv is written in dry run** — every simulated open/close is logged with real
entry/exit prices and calculated PnL. After a dry run session you can open the CSV
and see exactly what would have happened live.

**To switch to live:** change `DRY_RUN=true` → `DRY_RUN=false` in `.env`. No other
changes needed — all logic is identical.

---

## Aster funding (official docs vs bot)

Official mechanics and formulas: [Funding Rate — Aster perpetuals](https://docs.asterdex.com/trading/perpetuals/fees-and-specs/funding-rate). Highlights that matter for this repo:

- **Interval `N` is not always 8 hours** per symbol; Aster may change interval, floor, or cap. The bot **infers** the period in milliseconds when `nextFundingTime` advances between REST polls and uses **`24h / period`** for simple APR (until then it assumes **3 fundings/day**).
- **Settlement timing:** Aster documents a **~15 second** boundary around funding charges (entries just after an interval boundary may still pay/receive for that interval).
- **REST `lastFundingRate`:** `GET /fapi/v1/premiumIndex` returns the **last settled** rate (per Aster API examples), not necessarily the next interval’s predicted rate. Ranking, `MIN_FUNDING_RATE`, and default exits all use that series for consistency.
- **Optional WebSocket estimate:** `aster_ws.MarkPriceWatcher` records stream field **`r`** when present (Binance-style). Set `FUNDING_EXIT_USE_WS_ESTIMATED=true` to prefer **`r`** for `funding_dropped` when subscribed; otherwise REST `lastFundingRate` is used.
- **Sign vs wallet:** Aster’s prose defines who pays whom for a **positive** published rate; the bot periodically **compares** recent `FUNDING_FEE` rows from `GET /fapi/v1/income` with `lastFundingRate` for open symbols (see `FUNDING_SIGN_SELF_CHECK_CYCLES`). Validate thresholds against your own realized income.
- **CSV / dashboard:** Column `funding_rate_8h` is a legacy name; stored values are **percent per API funding interval**. APR columns use the learned (or default 8h) fundings-per-day multiplier.

## Staged live run (test → full)

Use the same code path while limiting risk:

1. **Stage A (paper on live chain):** `DRY_RUN=true`, `DRY_RUN_SIMULATED_MARGIN_USD=2000` (or another USD paper balance), `DRY_RUN_SHOW_LIVE_WALLET_DETAILS=false` — live rates/marks and signed GETs, sizing from paper margin, no orders. Alternatively `DRY_RUN_SIMULATED_MARGIN_USD=0` sizes from your real API margin while still simulating fills. Optional: `python funding_farmer.py --max-cycles 1` runs one full poll cycle then exits **without** closing positions (good for a quick connectivity + log check).
2. **Stage B (small live):** `DRY_RUN=false`, set `WALLET_MAX_USD` to a modest cap (e.g. 150–500 USDT notional budget ceiling), optionally lower `MAX_POSITIONS` to 1–3.
3. **Stage C (full):** `WALLET_MAX_USD=0` to remove the cap; restore `MAX_POSITIONS` / rank caps as desired. For Hyperliquid, prefer staging with `DRY_RUN=true` first.

Spot balances (USDF, ASTER, etc.) are **collateral**, not a separate sizing knob — see `.env.example` “Multi-Asset Margin” and “Staged live run”.

---

## Delta-neutral flag

`DELTA_NEUTRAL=false` in `.env` (default) — bot runs Aster-only, no HL connection needed.

Set `DELTA_NEUTRAL=true` to enable the HL hedge leg. When enabled, `funding_farmer.py`
imports `hl_setup`, `hl_open_short`, `hl_close_short`, `hl_get_funding_rate` from
`delta_neutral.py` at runtime. If the import or HL connection fails, the bot logs a
warning and continues in Aster-only mode gracefully.

**Gate logic summary:**
- Stop loss close → also closes HL short if `DELTA_NEUTRAL=true`
- Funding flip exit → also closes HL short if `DELTA_NEUTRAL=true`
- New position open → opens HL short first; skips the Aster long if HL fails
- Shutdown → closes both legs if `DELTA_NEUTRAL=true`

---

## Full .env reference

```env
# Credentials — Pro API V3 (recommended)
ASTER_USER=                  # Main wallet (0x...)
ASTER_SIGNER=                # API agent wallet from Pro API registration
ASTER_SIGNER_PRIVATE_KEY=    # Private key of the API agent (hex)

# Legacy HMAC (optional)
# ASTER_API_KEY=
# ASTER_SECRET_KEY=

# Core risk
LEVERAGE=3                   # Leverage per position (keep low for funding farms)
MIN_FUNDING_RATE=0.0005      # Min 0.05%/8h (~54% APR) to enter
EXIT_FUNDING_RATE=0.0001     # Exit if rate falls below 0.01%/8h
STOP_LOSS_PCT=0.05           # 5% drawdown closes position
POLL_INTERVAL_SEC=300        # Scan every 5 minutes
BLACKLIST=                   # Comma-sep symbols to never trade

# Wallet-based auto-sizing (replaces fixed POSITION_USDT)
WALLET_DEPLOY_PCT=0.80       # Deploy 80% of effective margin as notional
WALLET_MAX_USD=0             # Hard ceiling (0 = off). Set e.g. 500 while testing
WALLET_MIN_USD=20            # Don't open below $20 (avoids min-order errors)

# Diversification
MAX_POSITIONS=7              # Max concurrent open positions
RANK_TOP_PCT=0.25            # Top-ranked symbol gets 25% of deploy budget
MAX_SINGLE_PCT=0.30          # Hard per-symbol cap (30% of budget)
CORR_GROUPS=BTCUSDT|WBTCUSDT,ETHUSDT|STETHUSDT|WETHUSDT

# Logging
TRADE_LOG_FILE=trades.csv    # CSV trade log path

# Delta-neutral only (delta_neutral.py) — HL is always mainnet
HL_PRIVATE_KEY=              # Hyperliquid private key (hex)
HL_WALLET_ADDRESS=           # HL wallet address
LEVERAGE_HL=3                # Leverage on HL short leg
HEDGE_RATIO=1.0              # 1.0 = 100% delta neutral
MIN_NET_FUNDING=0.0002       # Min Aster-HL spread to enter
# DRY_RUN applies to both Aster (funding_farmer.py) and HL (delta_neutral.py) — see Dry run mode above
DRY_RUN=true                 # testing: true = no real orders; production: set false
```

---

## Wallet-based sizing — how it works

Every cycle:
1. `get_collateral_summary()` fetches live balances → computes effective margin
   - USDT: 100%
   - USDF: 99.99%
   - ASTER: 80% (your 2000 ASTER contribute ~$1,120 at $0.70)
2. `compute_deploy_budget()` = effective_margin × WALLET_DEPLOY_PCT (capped by WALLET_MAX_USD)
3. `available_budget()` = total_budget − already deployed notional
4. `rank_weighted_sizes()` splits available budget across new candidates

**Compounding effect:** as funding carry accumulates in your wallet, the next batch of
positions automatically opens larger. No config change needed.

---

## Diversification logic

**Rank-weighted sizing** (`rank_weighted_sizes`):
- Candidates sorted by funding rate descending (already done by `get_all_funding_rates`)
- Symbol #1 (highest rate) gets `RANK_TOP_PCT` × budget
- Symbols #2..N split the remainder equally
- Each symbol capped at `MAX_SINGLE_PCT` × budget

**Correlation guard** (`is_correlated`):
- Uses incremental `pending` set — symbols selected earlier in the same scan cycle
  count as "open" for correlation purposes, preventing same-cycle correlated opens
- Defined in `CORR_GROUPS` env var, pipe-sep within group, comma-sep between groups

**Example with $1000 wallet, 80% deploy, 4 candidates:**
```
budget = $800
BTC (#1): $800 × 0.25 = $200
ETH (#2): $800 × 0.75 / 3 = $200
SOL (#3): $200
BNB (#4): $200
Total: $800 ✓
```

---

## Trade log (trades.csv)

Every OPEN and CLOSE writes a row:

| Column | OPEN | CLOSE |
|---|---|---|
| timestamp_utc | ✓ | ✓ |
| action | OPEN | CLOSE |
| symbol | ✓ | ✓ |
| order_id | ✓ | ✓ |
| quantity | ✓ | ✓ |
| price | live: avg entry fill; dry: mark | live: avg exit fill; dry: mark |
| notional_usdt | ✓ | ✓ |
| funding_rate_8h | rate at entry | rate at exit |
| funding_apr_pct | ✓ | ✓ |
| entry_price | — | from `_open_trades`, else position `entryPrice` |
| exit_price | — | ✓ |
| fee_entry_usdt | open commission (USDT) | same leg repeated for convenience |
| fee_exit_usdt | — | close commission (USDT) |
| fees_usdt | — | entry + exit trading fees |
| pnl_gross_usdt | — | (exit−entry) × qty before fees |
| pnl_usdt | — | **net** after trading fees (gross − fees) |
| pnl_pct | — | net PnL % vs entry notional |
| hold_duration_min | — | time.time() diff |
| close_reason | — | stop_loss / funding_dropped / shutdown |

Fees come from `GET /fapi/v1/userTrades` (commissions converted to USDT where needed).
**Funding** paid/received over the hold is **not** included in `pnl_usdt` — use exchange funding history for that.

`_open_trades` holds entry avg + open fee for positions opened in-process. After a restart,
entry/fee for the open leg may be missing; close still uses the exchange `entryPrice` for
gross/net math when the cache is empty (open fee may be 0 in that case).

---

## Aster Stage 6 points scoring

Points formula: `(Trading + Position + AsterAsset + Liquidation + PnL) × TeamBoost + Referral`

This bot hits 4 out of 5 categories:

| Category | How the bot earns it |
|---|---|
| Trading Points | Entry + exit fees (taker = 2× maker) |
| Position Points | Large notional × hold time (no cap in Stage 6) |
| Aster Asset Points | USDF + ASTER held as margin (automatic, no extra trades) |
| PnL Points | Positive funding carry (updated hourly) |

**Important:** Aster disqualifies bot-registered accounts and wash trades.
This bot uses your real account with genuine market activity — that's fine.
Do not run multiple accounts to farm extra points.

---

## delta_neutral.py — architecture

Extends the funding farm with a Hyperliquid short hedge leg:

```
run()
├── aster_enable_multi_asset_mode()
├── hl_setup()  →  Info + Exchange + address
│
└── while True:
    ├── aster_get_funding_rates()        scan Aster
    ├── hl_get_funding_rate(coin)        scan HL for same symbol
    ├── net_rate = aster_rate - hl_rate  must exceed MIN_NET_FUNDING
    │
    ├── hl_open_short(coin, notional)    HL MARKET short (hedge leg)
    │   └── if fails → skip entirely    never open Aster unhedged
    ├── aster_open_long(symbol, notional)
    │
    └── on exit:
        ├── aster_close_long(reason)
        └── hl_close_short(reason)       simulated HL shorts tracked in delta_neutral._dry_hl_short when DRY_RUN
```

**Symbol mapping:** `funding_farmer.py` passes the HL coin by stripping `USDT` from the Aster symbol (e.g. `BTCUSDT` → `BTC`).

**Dry run mode:** `DRY_RUN=true` in `.env` skips real orders on Aster (`funding_farmer.py`) and on Hyperliquid (`delta_neutral.py`); both still log and `trades.csv` records Aster legs.

---

## Common errors and fixes

| Error | Cause | Fix |
|---|---|---|
| `API error -2011: Unknown order sent` | reduceOnly on non-existent position | Check `get_positions()` before close |
| `No need to change` on marginType | Already set to CROSS | Handled gracefully, ignore |
| Quantity precision error | stepSize not respected | `round_step(qty, stepSize)` always used |
| `pnl=n/a` in CLOSE row | Bot restarted, no in-memory entry | Expected — entry data lost on restart |
| `Available budget $X below minimum $Y` | Wallet too small or all deployed | Increase wallet or wait for positions to close |
| HL `Could not get price for COIN` | Symbol not on HL | Add to BLACKLIST or use funding_farmer.py only |

---

## Deployment (Fly.io)

```bash
fly launch --name aster-farmer
fly secrets set ASTER_USER=0x... ASTER_SIGNER=0x... ASTER_SIGNER_PRIVATE_KEY=0x...
fly deploy
```

For delta_neutral.py, also set:
```bash
fly secrets set HL_PRIVATE_KEY=xxx HL_WALLET_ADDRESS=xxx
```

trades.csv and logs persist within the container. For persistent storage across
deploys, mount a Fly volume and set `TRADE_LOG_FILE=/data/trades.csv`.

---

## Extension ideas (not yet built)

- **Telegram alerts** — on open/close/stop-loss, send message via Bot API
- **Rebalancing** — if top symbol's funding rate drops but a better one appears,
  close the old and open the new (currently only closes on EXIT_FUNDING_RATE breach)
- **Funding forecast** — use rate trend over last 3 epochs to predict if rate
  will hold, skip entries with declining trend
