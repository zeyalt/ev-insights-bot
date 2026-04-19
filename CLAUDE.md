# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EV charging insights platform with two interfaces:
- **Telegram bot** (`bot/app.py`) — sends `/insights` (monthly summary), `/alltime` (all-time summary), and weekly scheduled reports
- **Web dashboard** (`bot/dashboard.html`) — single-file HTML/CSS/JS dashboard using Chart.js, served by the same Flask app

Data is sourced from a Google Sheets CSV (published URL in `CSV_URL` env var). Both interfaces parse the same CSV but independently — the bot uses pandas, the dashboard uses client-side JS.

## Running Locally

**Dashboard only (no Telegram):**
```bash
pip install flask requests
python test_server.py          # serves at http://localhost:10000
```

**Full bot (requires env vars):**
```bash
export TELEGRAM_TOKEN=... CHAT_ID=...
pip install -r bot/requirements.txt
python bot/app.py
```

## Architecture

### Data Flow
Google Sheets CSV → `/api/data` Flask endpoint → parsed client-side (dashboard) or server-side via pandas (bot)

### Key Design Decisions
- **All cost metrics use gross cost** (before rebates), not net. This applies to $/kWh in KPIs, tables, charts, and bot messages. The `cost_per_kwh` column in pandas is `gross_cost / kwh`.
- **Currency conversion**: MYR amounts are converted to SGD at `1 / 3.14`. Applied in both `app.py` and `dashboard.html`.
- **Energy consumption** (kWh/100km) is derived from consecutive charging sessions: battery drop between sessions × kWh-per-percent, divided by odometer distance. Outliers are removed via IQR in the bot; the dashboard shows raw values.
- **$/kWh is always 4 decimal places** throughout both interfaces.

### Dashboard (`bot/dashboard.html`)
Single-file ~1800 lines containing all HTML, CSS, and JS. Key patterns:
- Chart.js v4.4.0 for all visualizations (bar, doughnut, line)
- Light/dark theme toggle with CSS variables; donut center text detects theme dynamically
- Zone-based layout: ZONE 2 (expenses donut + KPIs), ZONE 3 (provider donuts + heatmap), ZONE 4 (monthly bars), etc.
- `updateAll()` is the main render entry point, called on data load and filter changes
- Cross-filtering: clicking donut segments sets `crossFilterProvider`/`crossFilterType` and re-renders everything
- Provider colors shared across all provider-related charts via `providerColors` array

### Bot (`bot/app.py`)
- `fetch_data()` returns `(charging_df, expenses_df)` — all parsing and currency conversion happens here
- `build_insights()` generates the monthly Telegram message
- `compute_energy_consumption()` calculates avg kWh/100km with IQR outlier removal
- Flask runs in a background thread for Render health checks; Telegram bot uses polling
- APScheduler sends weekly reports (configurable day/hour via env vars)

## Deployment
Deployed on Render free tier via Docker (`bot/Dockerfile`). Config in `bot/render.yaml`. Required env vars: `TELEGRAM_TOKEN`, `CHAT_ID`. Optional: `CSV_URL`, `WEEKLY_DAY`, `WEEKLY_HOUR`, `PORT`.
