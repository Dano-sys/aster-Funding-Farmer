# Aster funding farmer — playbook

Reference for this repo (**Aster futures `/fapi` + Pro API V3 EIP-712**, optional **Hyperliquid** short hedge). Use it for auth, env, dry-run paper mode, and reusable `exchange.py` / `config.py` primitives.

**Networks:** Aster REST/WS and Hyperliquid are **mainnet only**. There is no Aster or HL testnet switch in this codebase. Use **`DRY_RUN=true`** to exercise live reads without sending orders.

---

## Scope

- **In scope:** `funding_farmer.py` (multi-symbol funding farm, paper or live), `exchange.py` (signed REST, ladder helpers, flatten), `config.py`, `delta_neutral.py` (HL hedge, mainnet), supporting CLIs (`profit_assistant.py`, `trade_smoke_test.py`, etc.).
- **Out of scope here:** Aster spot/margin/pools bots (checklist placeholders at the end only).

---

## Wallet + keys (do this first)

### Required for signed Aster `/fapi` calls

- **`ASTER_USER`**: login / main wallet address on Aster.
- **`ASTER_SIGNER`**: API signer (agent) wallet address.
- **`ASTER_SIGNER_PRIVATE_KEY`**: hex private key for `ASTER_SIGNER`.

**`ASTER_USER` and `ASTER_SIGNER` are not assumed to be the same.** Wrong `ASTER_USER` often yields HTTP 400 “No aster user found”.

Optional:

- **`STRIP_0X_PREFIX_FROM_KEYS=true`**: if a pasted key’s `0x` is not part of the real key material (handled in `config.py`).

### Hyperliquid hedge (`DELTA_NEUTRAL=true`)

- **`HL_PRIVATE_KEY`**, **`HL_WALLET_ADDRESS`** (optional if the key’s default address is the trading wallet). HL API URL is always mainnet (`delta_neutral.py`).

---

## Files (source of truth)

| File | Role |
|------|--------|
| [`config.py`](config.py) | Env loading, defaults, `BASE_URL`, WS URL, `DRY_RUN`, `DRY_RUN_PAPER_FILLS`, `TRADING_HALTED`, ladder/collateral knobs |
| [`exchange.py`](exchange.py) | Aster futures REST + signing, market WS fallback, `get_balance()`, ladder placement, flatten, webhook alerts |
| [`funding_farmer.py`](funding_farmer.py) | Funding-rate farmer loop, `get_collateral_summary()`, deploy budget, dry-run simulated positions |
| [`delta_neutral.py`](delta_neutral.py) | HL short leg (mainnet only) |
| [`.env.example`](.env.example) | Non-secret template and staged run comments |

**Paper simulation:** there is no separate `paper.py`. Dry-run fills and in-memory positions live in **`funding_farmer.py`**; ladder dry-run logging lives in **`exchange.py`** when that path is used.

---

## Local setup

This repo loads **`.env`** and **`env`** from the repo root (see `config.py` / `funding_farmer.py`).

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env: ASTER_USER, ASTER_SIGNER, ASTER_SIGNER_PRIVATE_KEY
python funding_farmer.py
```

Start with **`DRY_RUN=true`**. For **paper balances on the live chain**, set **`DRY_RUN_SIMULATED_MARGIN_USD`** to a positive USD value (see `.env.example` Stage A).

---

## Env catalog (this repo)

### Secrets (never commit)

- **`ASTER_USER`**, **`ASTER_SIGNER`**, **`ASTER_SIGNER_PRIVATE_KEY`**
- **`HL_PRIVATE_KEY`**, **`HL_WALLET_ADDRESS`** (when `DELTA_NEUTRAL=true`)
- **`ALERT_WEBHOOK_URL`** (optional; Slack-style `{"text":"..."}` — used in `exchange.py`)

### Market plumbing (mainnet)

- **`ASTER_FAPI_BASE`**: override REST host (default `https://fapi.asterdex.com` in `config.py`).
- **Unsigned futures WebSocket** (`config.py`):
  - `FUTURES_WS_ENABLED` (default true)
  - `FUTURES_WS_URL` (optional override; default `wss://fstream.asterdex.com/ws`)
  - `FUTURES_WS_FALLBACK_AFTER_SEC`

There is **no** `TESTNET` or Aster testnet host env var.

### Safety / modes

- **`DRY_RUN`**: skip Aster POSTs that open/close/change margin (see `funding_farmer.py`); HL orders skipped when true in `delta_neutral.py`.
- **`DRY_RUN_SIMULATED_MARGIN_USD`**: when `DRY_RUN` and value **> 0**, **`_total_effective_margin`** for sizing is this USD amount (live funding/marks still used). **`0`** = size from live balance-derived margin.
- **`DRY_RUN_SHOW_LIVE_WALLET_DETAILS`**: print live futures/spot tables during dry run; when simulated margin > 0 and this is true, a **`PAPER`** row is prepended to the futures table.
- **`DRY_RUN_PAPER_FILLS`**: when `DRY_RUN`, ladder path in `exchange.py` can simulate fills vs last price (default true in `config.py`).
- **`FARMING_HALT`** / **`FARMING_HALT_FILE`**: stop **new** opens in the farmer; exits unchanged.
- **`TRADING_HALTED`** / **`TRADING_HALT_FILE`**: halt destructive actions in **`exchange.py`** ladder path.
- **`CANCEL_OPEN_ORDERS_ON_STARTUP`**, **`CLOSE_POSITION_ON_STARTUP`**, **`BOT_STATE_PATH`**: see `config.py` / `exchange.py` usage.

### Sizing / collateral

- **`BALANCE_SIZING_SCOPE`**: `collateral` | `all_wallet` (`config.py` + `exchange.get_balance()`).
- **`COLLATERAL_ASSETS`**, **`COLLATERAL_ASSET`**, **`COLLATERAL_PRICE_SYMBOL`**
- **`BALANCE_LOG_DUST_MIN_USD`** (`exchange.py` logging / dust for `all_wallet` style sums)

### Funding farmer (see `.env.example`)

- **`MAX_POSITIONS`**, **`MIN_FUNDING_RATE`**, **`EXIT_FUNDING_RATE`**, **`WALLET_DEPLOY_PCT`**, **`WALLET_MAX_USD`**, **`LEVERAGE`**, **`DELTA_NEUTRAL`**, **`MIN_NET_FUNDING`**, etc.

### Ladder / news knobs (`config.py`)

`SYMBOL`, `LEVERAGE`, `WALLET_PCT`, zone/news envs apply to the **ladder + news** strategy in `exchange.py`, not the core funding farmer sizing (which uses `WALLET_DEPLOY_PCT` and collateral summary).

---

## Aster `/fapi` capabilities (reuse)

Implemented in **`exchange.py`** (and partially duplicated for the farmer in `funding_farmer.py` / `aster_client`):

- **Auth:** EIP-712 Pro API V3; logical `/fapi/v1|v2` paths rewritten to `/fapi/v3` where required.
- **Market:** `GET /fapi/v1/ticker/price`, `bookTicker`, `premiumIndex`, WebSocket fallbacks.
- **Account:** `GET /fapi/v2/balance`, `GET /fapi/v2/account` (available balance cap), `GET /fapi/v2/positionRisk`.
- **Setup:** `marginType`, `leverage`, `multiAssetsMargin`.
- **Orders:** `POST /fapi/v1/order`, `DELETE /fapi/v1/allOpenOrders`, `GET /fapi/v1/openOrders`.
- **Flatten:** market close + poll / chase (see `flatten_position_for_symbol`).

---

## Deployment

- **Local / VPS:** run `python funding_farmer.py` from the repo root; provide secrets via `.env` or process env.
- **Fly.io / Docker:** if you add `fly.toml` / `Dockerfile`, keep private keys in **`fly secrets set`**, not in `[env]`. Align non-secret `[env]` with `.env.example` where applicable.

---

## Troubleshooting

### HTTP 400: “No aster user found”

Fix **`ASTER_USER`** (main login wallet, not the API signer).

### HTTP 401/403

- IP allowlist on Aster (if any).
- Key formatting (BOM, quotes, newlines); **`STRIP_0X_PREFIX_FROM_KEYS`** only when appropriate.

### Insufficient margin / -2019

- Deploy fraction / leverage too aggressive; raise **`LADDER_IM_HEADROOM_PCT`** for ladder bots.
- For “whole wallet” style sizing, consider **`BALANCE_SIZING_SCOPE=all_wallet`** and UM **`availableBalance`** cap behavior in `exchange.get_balance()`.

---

## Recipes

### Paper on live chain (this bot)

1. `DRY_RUN=true`
2. `DRY_RUN_SIMULATED_MARGIN_USD=2000` (or your test notional base)
3. Optional: `DRY_RUN_SHOW_LIVE_WALLET_DETAILS=false` for minimal logs
4. `python funding_farmer.py --max-cycles 1` for a single-cycle smoke test

### New futures bot from this toolkit

Import or copy **`config.py`** + **`exchange.py`**. Use **`get_balance()`**, **`get_book_ticker`**, signed GETs/POSTs, **`flatten_position_for_symbol`**, and respect **`DRY_RUN`** / **`TRADING_HALTED`** the same way.

---

## Placeholders (other Aster products)

Checklist only — not implemented here.

### Spot / margin / pools

Confirm auth, balances, order types, and risk models per Aster product docs before building dedicated bots.
