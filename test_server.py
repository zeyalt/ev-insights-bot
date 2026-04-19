#!/usr/bin/env python3
"""Simple Flask server for testing the dashboard (Telegram bot disabled)"""

import os
import io
import requests
from flask import Flask, send_file

app = Flask(__name__, static_folder='bot')

# CSV configuration
CSV_URL = os.environ.get(
    "CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vSwMAeGbr71UIP91NTDY_-NNnsKrFiEEGC6vFspwBBFqgbLdkzDwCZLVLEheLoJJlcO-1cDdiyuu5_t/pub?output=csv",
)

@app.route("/")
@app.route("/dashboard")
@app.route("/dashboard.html")
def serve_dashboard():
    """Serve the dashboard HTML file."""
    return send_file("bot/dashboard.html", mimetype="text/html")

@app.route("/api/data")
def get_csv_data():
    """Provide CSV data for the dashboard."""
    try:
        resp = requests.get(CSV_URL, timeout=30)
        resp.raise_for_status()
        return resp.text, 200, {"Content-Type": "text/csv"}
    except Exception as e:
        return {"error": str(e)}, 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"\n🚀 Dashboard test server running at http://localhost:{port}")
    print(f"📊 Open http://localhost:{port} in your browser\n")
    app.run(host="0.0.0.0", port=port, debug=True)
