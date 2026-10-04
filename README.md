# Production-Ready 24/7 NFT Collection Monitoring & Filtering Bot (Stage 1)

A high-performance, deterministic backend bot that continuously monitors NFT collections on OpenSea, ingests objective market data, calculates metrics over complete calendar days, applies rigorous deterministic filters, computes hypothetical trade economics, and outputs complete factual handoff dossiers (`Info.md`) for qualifying candidate projects.

---

## 1. Architectural Principles & Handoff Contract

This bot represents **Stage 1 (BOT)** in a two-stage pipeline:

```
[Stage 1: BOT (This System)]
   │
   ├─ Continuous 24/7 Monitoring & Progressive Discovery
   ├─ Factual Data Ingestion (OpenSea v2 API)
   ├─ Calendar-Day Sales & Floor Calculations
   ├─ Deterministic Filter Engine (No LLM, No AI Reasoning)
   └─ Atomic Candidate Handoff: DATA_ROOT/YYYY-MM-DD/<Project>/Info.md
                                       │
                                       ▼
                     [Stage 2: OpenClaw + AI (Future)]
                        │
                        ├─ Filesystem Watcher detects: Info.md exists AND Fundamentals.md missing
                        ├─ Qualitative Web & Social Research
                        └─ Creates: Fundamentals.md
```

- **Strictly Deterministic**: No LLM, no AI reasoning, no subjective social sentiment analysis.
- **Strict Output Boundary**: Creates **only** `Info.md` inside candidate directories. `Fundamentals.md` is **never created** by this bot.
- **Fail-Closed Principle**: Missing, stale, or incomplete data results in `FAIL / DATA_INSUFFICIENT`. Missing data is never treated as zero.

---

## 2. Monitored Universe & Progressive Discovery

- **Primary Source**: `GET /api/v2/collections?chain=<chain>&order_by=seven_day_volume`, one persistent cursor per configured chain in SQLite (`state/bot.db`). Each chain's crawl restarts from the top after `max_depth_pages`, so the most-traded collections keep being rediscovered instead of the crawl drifting into the long tail of unverified collections.
- **Shortlist Refresh**: Collections that pass the cheap structural filters (age, verification, listings, trading frequency) are re-evaluated every `shortlist_refresh_seconds` (default hourly), which also builds their floor history.
- **Supplementary Sources**: a custom `watchlist` in `config/config.yaml`. `GET /api/v2/collections/top` and `/trending` are off by default because they are not documented v2 endpoints.
- **Batch Optimization**: Uses `POST /api/v2/collections/batch` to fetch metadata for multiple collections in single requests where applicable.

---

## 3. Deterministic Filtering Rules

All filters are centrally configured in `config/config.yaml`:

| # | Filter | Threshold | Evaluation Metric | Details |
|---|--------|-----------|-------------------|---------|
| 1 | **Trading Frequency** | $\le 2.0$ trades/day | `average_daily_sales` (default) | Grouped across **7 complete calendar days** in local timezone (`Asia/Kolkata`). Deduplicates sales by transaction/order hash. Tracks both transaction count and items sold. |
| 2 | **1-Day Floor Price Change** | $< 8.0\%$ | `abs((current - floor_24h_ago) / floor_24h_ago) * 100` | Reference floor comes from the bot's own floor snapshots (see below). |
| 3 | **7-Day Floor Price Change** | $< 10.0\%$ | `abs((current - floor_7d_ago) / floor_7d_ago) * 100` | Reference floor comes from the bot's own floor snapshots (see below). |
| 4 | **Floor vs Top Offer Spread** | $\ge 40\%$ | `(floor - effective_entry_cost) / effective_entry_cost * 100` | "Floor above 40% of top offer, including royalty". `effective_entry_cost` = top collection offer + 1% premium + creator royalty owed on resale. Other formulas selectable via `filters.offer_to_floor.formula`. |
| 5 | **Listed Items** | $< 6.0\%$ | `(unique_listed_nfts / total_supply) * 100` | Several listings of the same NFT count once. **Early-exit optimization**: paginator aborts immediately if active listings exceed the threshold. |
| 6 | **OpenSea Verification** | Blue Checkmark | `safelist_status in ["verified"]` | Specifically requires `verified` (blue tick); rejects `approved` or unverified collections. |
| 7 | **OpenSea Collection Age**| $> 60.0$ days | `(detection_date - created_date).days` | OpenSea collection age calculated from `created_date`. |
| 8 | **Minimum Net Profit** | $\ge 10\%$ net ROI | `net_profit / entry_offer * 100` | After marketplace fee, creator royalty and gas. Fails closed when the trade model is incomplete. |

### Floor history

OpenSea v2 has no documented floor-price time-series endpoint, so every evaluation stores the current floor (from `/collections/{slug}/stats`) in the `floor_snapshots` table. The 1-day check uses the snapshot nearest to 24h ago (±6h) and the 7-day check the snapshot nearest to 7 days ago (±24h). Until those exist, collections are rejected as `floor_history_1d` / `floor_history_7d`, so **a freshly started bot cannot produce candidates for about 7 days**. Keep `state/bot.db` between restarts.

### Rejection funnel

Every evaluation records which filter rejected the collection. `python -m bot status` prints the counts for the last 7 days, which shows which rule is the bottleneck.

---

## 4. Theoretical Trade Economics Model

The bot computes hypothetical entry-and-exit trade economics while clearly distinguishing:
- **Observed Market Data**: Current floor price, highest collection-wide offer (per NFT; item and trait offers are ignored), creator royalty fee %, OpenSea marketplace fee % (from collection metadata, or `trade_model.marketplace_fee_pct` labelled CONFIGURED when metadata does not list it).
- **Model Assumptions**: Modelled entry offer premium (+1.0%), target exit discount below floor (-5.0%), estimated gas cost (0.005 ETH).
- **Modelled Results**: Modelled entry offer, target exit price, gross spread, separate marketplace & creator fees, net profit, ROI, and profit margin.

> **Note**: If the OpenSea fee is not in the collection metadata, the configured `trade_model.marketplace_fee_pct` is used and labelled `CONFIGURED` in Info.md. Set it to `null` to mark such collections `INCOMPLETE / UNKNOWN` instead (they then fail the net-profit filter).

---

## 5. Quick Start & Setup

### Requirements
- Python 3.9+
- Virtual environment (`.venv`)

### Installation
```bash
# 1. Initialize virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure API key
cp .env.example .env
# Edit .env with your OpenSea API key:
# OPENSEA_API_KEY=your_key_here
```

### Running on a Mac (recommended)
See **[docs/MAC_SETUP.md](docs/MAC_SETUP.md)**: iMac power settings, install, and `python bot.py install-autostart`
so the dashboard opens at login and keeps the bot running after power cuts and crashes.

### Dashboard
```bash
python bot.py ui            # http://127.0.0.1:5050
```
Pages: Home (status, checks today, rejection funnel, activity), Candidates, Near misses,
Check a collection, and Settings. Settings switches each rule on or off (a switched-off rule is still
measured but never rejects), edits limits, trade model, chains and per-chain gas. Changes are saved to
`config/overrides.yaml`, which overrides `config/config.yaml`, and the running bot picks them up at its next check.

### Chains
The bot scans the 27 OpenSea NFT chains listed in `src/config/chains.py` (every chain except Solana).
Each chain has its own currency group (a coin and its wrapped form count as the same, e.g. APE/WAPE)
and a default gas per trade in its own coin. Collections priced in a currency that doesn't match the
top offer's (e.g. USDG items with WETH offers) fail closed.

### Running the Bot

#### Continuous 24/7 Daemon
```bash
python -m bot
```

#### Dry-Run Mode (Simulation without writing files)
```bash
python -m bot --dry-run
```

#### Diagnostic Inspection of a Single Collection
Evaluates and prints all fetched market data, metrics, trade economics, and filter results:
```bash
python -m bot inspect doodles-official
# Or by full OpenSea URL:
python -m bot inspect https://opensea.io/collection/boredapeyachtclub
```

#### Health & Status Check
```bash
python -m bot status
```

#### Running the Test Suite
```bash
pytest tests -v
```

---

## 6. Output Directory Structure & Atomic Writing

When a collection passes all deterministic filters, the bot writes:
```
data/
└── 2026-09-18/
    └── Cyber-Samurai-Origin/
        └── Info.md
```

- **Folder Naming**: Sanitizes illegal filesystem characters (`/ \ : * ? " < > |` replaced with `-`).
- **Atomic Renaming**: Writes to `Info.md.tmp` $\to$ `flush()` $\to$ `fsync()` $\to$ `os.replace("Info.md.tmp", "Info.md")` to guarantee external filesystem watchers never observe a partial file.
- **Daily Deduplication**: Tracks candidate evaluations in SQLite (`state/bot.db`) to ensure the same collection is never duplicated on the same calendar day.
