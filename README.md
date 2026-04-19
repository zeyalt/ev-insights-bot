# EV Insights Bot

A personal EV charging analytics platform that tracks charging sessions, costs, and energy consumption for an electric vehicle.

## Features

- **Telegram Bot** — monthly and all-time charging summaries on demand, plus automated weekly reports
- **Web Dashboard** — interactive charts and tables covering spend, energy, provider efficiency, battery behaviour, and more

## Data Source

Charging session data is logged via Google Forms into a Google Sheet, published as CSV. Both the bot and dashboard consume this CSV — the bot parses it server-side with pandas, the dashboard parses it client-side in JavaScript.

## Local Development

Run the dashboard locally (no Telegram credentials needed):

```bash
pip install flask requests
python test_server.py
```

Open http://localhost:10000 in your browser.

## Deployment

Deployed on Render free tier via Docker. See `bot/render.yaml` for config.

Required environment variables: `TELEGRAM_TOKEN`, `CHAT_ID`
Optional: `CSV_URL`, `WEEKLY_DAY`, `WEEKLY_HOUR`, `PORT`
