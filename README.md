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

- **Primary Source**: `GET /api/v2/collections` with persistent cursor checkpointing in SQLite (`state/bot.db`). The bot progressively crawls catalog pages without biasing toward high-volume collections.
- **Supplementary Sources**: `GET /api/v2/collections/top`, `GET /api/v2/collections/trending`, and a custom `watchlist` in `config/config.yaml`.
- **Batch Optimization**: Uses `POST /api/v2/collections/batch` to fetch metadata for multiple collections in single requests where applicable.

---

## 3. Deterministic Filtering Rules

All filters are centrally configured in `config/config.yaml`:

| # | Filter | Threshold | Evaluation Metric | Details |
|---|--------|-----------|-------------------|---------|
| 1 | **Trading Frequency** | $\le 2.0$ trades/day | `average_daily_sales` (default) | Grouped across **7 complete calendar days** in local timezone (`Asia/Kolkata`). Deduplicates sales by transaction/order hash. Tracks both transaction count and items sold. |
| 2 | **1-Day Floor Price Change** | $< 8.0\%$ | `abs((current - floor_24h_ago) / floor_24h_ago) * 100` | Evaluated against OpenSea time-series floor history (`timeframe=one_day`). |
| 3 | **7-Day Floor Price Change** | $< 10.0\%$ | `abs((current - floor_7d_ago) / floor_7d_ago) * 100` | Evaluated against OpenSea time-series floor history (`timeframe=seven_days`). |
| 4 | **Top Offer vs Floor** | Advisory ($\ge 40\%$) | Configurable | Flagged as advisory/observe-only by default pending final trading strategy formula confirmation. |
| 5 | **Listed Items** | $< 6.0\%$ | `(listed_items / total_supply) * 100` | **Early-exit optimization**: paginator aborts immediately if active listings exceed the threshold. |
| 6 | **OpenSea Verification** | Blue Checkmark | `safelist_status in ["verified"]` | Specifically requires `verified` (blue tick); rejects `approved` or unverified collections. |
| 7 | **OpenSea Collection Age**| $> 60.0$ days | `(detection_date - created_date).days` | OpenSea collection age calculated from `created_date`. |

---

## 4. Theoretical Trade Economics Model

The bot computes hypothetical entry-and-exit trade economics while clearly distinguishing:
- **Observed Market Data**: Current floor price, observed top offer, creator royalty fee %, OpenSea marketplace fee %.
- **Model Assumptions**: Modelled entry offer premium (+1.0%), target exit discount below floor (-5.0%), estimated gas cost (0.005 ETH).
- **Modelled Results**: Modelled entry offer, target exit price, gross spread, separate marketplace & creator fees, net profit, ROI, and profit margin.

> **Note**: If the applicable marketplace fee schedule cannot be determined reliably from the collection metadata, the model is explicitly marked as `INCOMPLETE / UNKNOWN` rather than assuming a fallback fee.

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
