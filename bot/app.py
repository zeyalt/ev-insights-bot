import os
import logging
import io
import threading
from datetime import datetime, timedelta

import pandas as pd
import requests
from flask import Flask, send_file, jsonify
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
CSV_URL = os.environ.get(
    "CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vSwMAeGbr71UIP91NTDY_-NNnsKrFiEEGC6vFspwBBFqgbLdkzDwCZLVLEheLoJJlcO-1cDdiyuu5_t/pub?output=csv",
)
MYR_TO_SGD = 1 / 3.14
WEEKLY_DAY = os.environ.get("WEEKLY_DAY", "mon")  # mon, tue, ...
WEEKLY_HOUR = int(os.environ.get("WEEKLY_HOUR", "9"))  # 24h SGT

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Data ingestion ───────────────────────────────────────────────────────────
def fetch_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch CSV from Google Sheets and return (charging_df, expenses_df)."""
    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))

    # Charging-related from form
    charging = df[df["What data do you want to record?"] == "Charging-Related"].copy()

    # Other Expenses entries (split into two groups)
    other_expenses_all = df[df["What data do you want to record?"] == "Other Expenses"].copy()

    # Rows with empty Expense Category are actually charging expenses
    other_expenses_all["Expense Category"] = other_expenses_all["Expense Category"].fillna("").astype(str).str.strip()
    charging_from_other = other_expenses_all[other_expenses_all["Expense Category"] == ""].copy()
    expenses = other_expenses_all[other_expenses_all["Expense Category"] != ""].copy()

    # Combine charging rows
    charging = pd.concat([charging, charging_from_other], ignore_index=True)

    # Parse charging fields
    charging["start"] = pd.to_datetime(
        charging["Charging Start Date & Time"], format="%d/%m/%Y %H:%M:%S", errors="coerce"
    )
    charging["end"] = pd.to_datetime(
        charging["Charging End Date & Time"], format="%d/%m/%Y %H:%M:%S", errors="coerce"
    )
    charging["kwh"] = pd.to_numeric(charging["Total kWh Charged"], errors="coerce").fillna(0)
    charging["odometer"] = pd.to_numeric(
        charging["Odometer (km) Before Charging"], errors="coerce"
    ).fillna(0)
    charging["batt_before"] = pd.to_numeric(
        charging["Battery Percentage Before Charging"], errors="coerce"
    ).fillna(0)
    charging["batt_after"] = pd.to_numeric(
        charging["Battery Percentage After Charging"], errors="coerce"
    ).fillna(0)

    # Cost conversion
    charging["gross_cost_raw"] = pd.to_numeric(
        charging["Charging Cost"].astype(str).str.replace(",", ""), errors="coerce"
    ).fillna(0)
    charging["rebate_raw"] = pd.to_numeric(
        charging["Rebate, if any"].astype(str).str.replace(",", ""), errors="coerce"
    ).fillna(0)
    charging["idle_fees_raw"] = pd.to_numeric(
        charging["Idle Fees"].astype(str).str.replace(",", ""), errors="coerce"
    ).fillna(0)

    is_myr = charging["Currency"].str.strip() == "MYR"
    charging["gross_cost"] = charging["gross_cost_raw"].where(~is_myr, charging["gross_cost_raw"] * MYR_TO_SGD)
    charging["rebate"] = charging["rebate_raw"].where(~is_myr, charging["rebate_raw"] * MYR_TO_SGD)
    charging["idle_fees"] = charging["idle_fees_raw"].where(~is_myr, charging["idle_fees_raw"] * MYR_TO_SGD)
    charging["net_cost"] = charging["gross_cost"] - charging["rebate"]
    charging["cost_per_kwh"] = charging["net_cost"] / charging["kwh"].replace(0, float("nan"))

    charging["duration_min"] = (charging["end"] - charging["start"]).dt.total_seconds() / 60
    charging["duration_hours"] = charging["duration_min"] / 60
    charging["charging_speed_kwh_per_hour"] = charging["kwh"] / charging["duration_hours"].replace(0, float("nan"))
    charging["battery_increase_pct_per_hour"] = (charging["batt_after"] - charging["batt_before"]) / charging["duration_hours"].replace(0, float("nan"))

    charging["month"] = charging["start"].dt.to_period("M")
    charging = charging.sort_values("start").reset_index(drop=True)

    # Distance between sessions
    charging["distance"] = charging["odometer"].diff().clip(lower=0).fillna(0)

    # Parse expenses
    expenses["date"] = pd.to_datetime(
        expenses["Expense Date"].astype(str) + " 00:00:00",
        format="%d/%m/%Y %H:%M:%S",
        errors="coerce",
    )
    expenses["amount"] = pd.to_numeric(
        expenses["Amount Paid (SGD)"].astype(str).str.replace(",", ""), errors="coerce"
    ).fillna(0)
    expenses["month"] = expenses["date"].dt.to_period("M")

    return charging, expenses


# ─── Analysis helpers ─────────────────────────────────────────────────────────
def month_filter(df, period):
    return df[df["month"] == period]


def build_insights(charging, expenses, period=None, prev_period=None):
    """Build a formatted insights message. If period is None, uses current month."""
    now = datetime.now()
    if period is None:
        period = pd.Period(now, freq="M")
    if prev_period is None:
        prev_period = period - 1

    cur_c = month_filter(charging, period)
    prev_c = month_filter(charging, prev_period)
    cur_e = month_filter(expenses, period)
    prev_e = month_filter(expenses, prev_period)

    # --- Expense distribution by category (combined: EV Charging + Other) ---
    by_category = {}

    # Add EV Charging as a category
    ev_charging_cost = charging["net_cost"].sum() + charging["idle_fees"].sum()
    if ev_charging_cost > 0:
        by_category["EV Charging"] = ev_charging_cost

    # Add other expense categories
    other_by_cat = expenses.groupby("Expense Category")["amount"].sum()
    for cat, amt in other_by_cat.items():
        by_category[cat] = by_category.get(cat, 0) + amt

    # Sort by amount descending
    sorted_cats = sorted(by_category.items(), key=lambda x: x[1], reverse=True)
    expense_lines = []
    for cat, amt in sorted_cats:
        expense_lines.append(f"  {cat}: ${amt:,.2f}")

    # --- MTD gross charging ---
    mtd_gross = cur_c["gross_cost"].sum()
    prev_gross = prev_c["gross_cost"].sum()

    # --- kWh MTD & MoM ---
    mtd_kwh = cur_c["kwh"].sum()
    prev_kwh = prev_c["kwh"].sum()

    # --- Efficiency: best providers & locations ---
    if len(charging) > 0:
        eff_provider = (
            charging.groupby("Charging Provider")
            .agg(avg_cpk=("cost_per_kwh", "mean"), sessions=("kwh", "count"), total_kwh=("kwh", "sum"))
            .sort_values("avg_cpk")
        )
        top_providers = eff_provider.head(3)

        eff_location = (
            charging.groupby("Charging Location")
            .agg(avg_cpk=("cost_per_kwh", "mean"), sessions=("kwh", "count"), total_kwh=("kwh", "sum"))
            .query("sessions >= 2")
            .sort_values("avg_cpk")
        )
        top_locations = eff_location.head(5)
    else:
        top_providers = pd.DataFrame()
        top_locations = pd.DataFrame()

    # --- Extra insights ---
    total_dist = charging["distance"].sum()
    total_kwh_all = charging["kwh"].sum()
    efficiency_km_per_kwh = total_dist / total_kwh_all if total_kwh_all > 0 else 0

    avg_charge_pct = (charging["batt_after"] - charging["batt_before"]).mean() if len(charging) > 0 else 0
    deep_discharge = charging[charging["batt_before"] <= 25]
    high_charge = charging[charging["batt_after"] >= 90]

    # Charging speed metrics
    avg_charging_speed = charging["charging_speed_kwh_per_hour"].mean() if len(charging) > 0 else 0
    avg_battery_rate = charging["battery_increase_pct_per_hour"].mean() if len(charging) > 0 else 0
    avg_duration_hours = charging["duration_hours"].mean() if len(charging) > 0 else 0

    # Most used location
    top_loc = charging["Charging Location"].value_counts().head(3) if len(charging) > 0 else pd.Series()

    # Subscription savings
    sub_sessions = charging[charging["Subscription Plan"].str.strip() != "None"]
    non_sub = charging[charging["Subscription Plan"].str.strip() == "None"]
    sub_avg = sub_sessions["cost_per_kwh"].mean() if len(sub_sessions) > 0 else 0
    non_sub_avg = non_sub["cost_per_kwh"].mean() if len(non_sub) > 0 else 0

    def delta_str(cur, prev, unit="", lower_better=False):
        if prev == 0:
            return "N/A (no prev data)"
        pct = ((cur - prev) / abs(prev)) * 100
        arrow = "\U0001F53C" if pct > 0 else "\U0001F53D" if pct < 0 else "\u27A1\uFE0F"
        good = (pct <= 0) if lower_better else (pct >= 0)
        indicator = "\u2705" if good else "\u26A0\uFE0F"
        return f"{cur:,.2f}{unit} ({arrow} {abs(pct):.0f}% vs prev month) {indicator}"

    period_label = period.strftime("%b %Y")
    prev_label = prev_period.strftime("%b %Y")

    msg = f"""
\U0001F50B *EV Insights \u2014 {period_label}*

\U0001F4B0 *Expense Distribution (All-Time)*
{chr(10).join(expense_lines)}

\u26FD *Gross Charging Spend*
  MTD: ${mtd_gross:,.2f}
  MoM: {delta_str(mtd_gross, prev_gross, lower_better=True)}

\u26A1 *Energy Consumption (kWh)*
  MTD: {mtd_kwh:,.1f} kWh
  MoM: {delta_str(mtd_kwh, prev_kwh, ' kWh')}

\U0001F3C6 *Most Efficient Providers (avg $/kWh)*"""

    for prov, row in top_providers.iterrows():
        msg += f"\n  {prov}: ${row['avg_cpk']:.3f}/kWh ({int(row['sessions'])} sessions)"

    msg += "\n\n\U0001F4CD *Most Efficient Locations (avg $/kWh, \u22652 sessions)*"
    for loc, row in top_locations.iterrows():
        msg += f"\n  {loc}: ${row['avg_cpk']:.3f}/kWh ({int(row['sessions'])} sessions)"

    msg += f"""

\U0001F4CA *Extra Insights*
  \U0001F698 Est. total distance: {total_dist:,.0f} km
  \u26A1 Avg efficiency: {efficiency_km_per_kwh:.1f} km/kWh
  \U0001F50B Avg charge gain per session: {avg_charge_pct:.0f}%
  \u231A Avg charging duration: {avg_duration_hours:.1f} hours
  \U0001F4A1 Avg charging speed: {avg_charging_speed:.1f} kWh/hour
  \U0001F4CB Avg battery increase rate: {avg_battery_rate:.1f}%/hour
  \U0001F7E2 Sessions starting \u226425%: {len(deep_discharge)} ({len(deep_discharge)/len(charging)*100:.0f}% of total)
  \U0001F534 Sessions charging to \u226590%: {len(high_charge)} ({len(high_charge)/len(charging)*100:.0f}% of total)"""

    if len(top_loc) > 0:
        msg += "\n  \U0001F3E0 Top charging spots:"
        for loc, cnt in top_loc.items():
            msg += f"\n    {loc}: {cnt}x"

    if sub_avg > 0 and non_sub_avg > 0:
        saving_pct = ((non_sub_avg - sub_avg) / non_sub_avg) * 100
        msg += f"\n  \U0001F4B3 Subscription vs pay-as-you-go: ${sub_avg:.3f} vs ${non_sub_avg:.3f}/kWh ({saving_pct:.0f}% saving)"

    idle_total = charging["idle_fees"].sum()
    if idle_total > 0:
        msg += f"\n  \u23F0 Total idle fees paid: ${idle_total:.2f}"

    return msg


# ─── Telegram handlers ────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001F50B *EV Insights Bot*\n\n"
        "Commands:\n"
        "/insights \u2014 Current month summary\n"
        "/insights YYYY-MM \u2014 Specific month\n"
        "/alltime \u2014 All-time summary\n",
        parse_mode="Markdown",
    )


async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        charging, expenses = fetch_data()
        period = None
        if context.args:
            try:
                period = pd.Period(context.args[0], freq="M")
            except Exception:
                await update.message.reply_text("Invalid format. Use: /insights YYYY-MM")
                return
        msg = build_insights(charging, expenses, period=period)
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Error in /insights")
        await update.message.reply_text(f"Error fetching insights: {e}")


async def cmd_alltime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        charging, expenses = fetch_data()
        total_net = charging["net_cost"].sum()
        total_idle = charging["idle_fees"].sum()
        total_exp = expenses["amount"].sum()
        total_kwh = charging["kwh"].sum()
        total_dist = charging["distance"].sum()
        n = len(charging)
        avg_cpk = total_net / total_kwh if total_kwh > 0 else 0

        by_month_c = charging.groupby("month").agg(
            net=("net_cost", "sum"), kwh=("kwh", "sum"), sessions=("kwh", "count")
        )
        by_month_e = expenses.groupby("month")["amount"].sum()

        month_lines = []
        for m in sorted(set(list(by_month_c.index) + list(by_month_e.index))):
            c = by_month_c.loc[m] if m in by_month_c.index else pd.Series({"net": 0, "kwh": 0, "sessions": 0})
            e = by_month_e.get(m, 0)
            month_lines.append(
                f"  {m}: ${c['net']+e:,.0f} ({int(c.get('sessions',0))} sessions, {c['kwh']:,.0f} kWh)"
            )

        msg = f"""\U0001F4CA *All-Time EV Summary*

\U0001F4B0 Total spend: ${total_net + total_idle + total_exp:,.2f}
  Charging (net): ${total_net:,.2f}
  Idle fees: ${total_idle:,.2f}
  Other expenses: ${total_exp:,.2f}

\u26A1 Total kWh: {total_kwh:,.1f}
\U0001F698 Est. distance: {total_dist:,.0f} km
\U0001F50B Sessions: {n}
\U0001F4B5 Avg cost/kWh: ${avg_cpk:.3f}

\U0001F4C5 *Monthly Breakdown*
{chr(10).join(month_lines)}"""

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Error in /alltime")
        await update.message.reply_text(f"Error: {e}")


# ─── Scheduled weekly summary ─────────────────────────────────────────────────
def send_weekly_sync():
    """Synchronous wrapper for the weekly scheduled job."""
    import asyncio

    async def _send():
        try:
            charging, expenses = fetch_data()
            msg = "\U0001F4C5 *Weekly Scheduled Report*\n" + build_insights(charging, expenses)
            app = Application.builder().token(TELEGRAM_TOKEN).build()
            async with app:
                await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            logger.info("Weekly summary sent successfully")
        except Exception as e:
            logger.exception(f"Failed to send weekly summary: {e}")

    asyncio.run(_send())


# ─── Flask keep-alive (Render free tier) ──────────────────────────────────────
flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return "EV Insights Bot is running", 200


@flask_app.route("/health")
def health_check():
    return "OK", 200


@flask_app.route("/dashboard")
@flask_app.route("/dashboard.html")
def serve_dashboard():
    """Serve the dashboard HTML file."""
    return send_file("dashboard.html", mimetype="text/html")


@flask_app.route("/api/data")
def get_csv_data():
    """Provide CSV data for the dashboard."""
    try:
        resp = requests.get(CSV_URL, timeout=30)
        resp.raise_for_status()
        return resp.text, 200, {"Content-Type": "text/csv"}
    except Exception as e:
        logger.error(f"Failed to fetch CSV: {e}")
        return jsonify({"error": str(e)}), 500


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Start Flask in a background thread for Render health checks
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))),
        daemon=True,
    )
    flask_thread.start()

    # Schedule weekly summary
    scheduler = BackgroundScheduler(timezone="Asia/Singapore")
    scheduler.add_job(
        send_weekly_sync,
        "cron",
        day_of_week=WEEKLY_DAY,
        hour=WEEKLY_HOUR,
        minute=0,
    )
    scheduler.start()
    logger.info(f"Weekly summary scheduled: every {WEEKLY_DAY} at {WEEKLY_HOUR}:00 SGT")

    # Start Telegram bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("alltime", cmd_alltime))

    logger.info("Bot starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
