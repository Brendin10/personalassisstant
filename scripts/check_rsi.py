"""
Checks SPY (S&P 500 ETF) 5-min RSI(14) via Twelve Data.
Emails an alert when RSI crosses below the threshold.
Run by .github/workflows/rsi-alert.yml.

Required environment variables (set as GitHub repo secrets):
  TWELVEDATA_KEY  - free API key from twelvedata.com
  GMAIL_ADDRESS   - Gmail address to send FROM
  GMAIL_APP_PASS  - Gmail app password (myaccount.google.com/apppasswords)
"""

import datetime as dt
import json
import os
import smtplib
import sys
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

ALERT_TO = "Brendunvnz@gmail.com"
SYMBOL = "SPY"
RSI_PERIOD = 14
RSI_THRESHOLD = 30
STATE_FILE = Path(".alert_state.json")  # cached between runs to avoid duplicate emails

ET = ZoneInfo("America/New_York")
# NYSE full-day holidays (update yearly)
MARKET_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
# Early closes at 1:00 pm ET (day after Thanksgiving, Christmas Eve)
EARLY_CLOSES = {"2026-11-27", "2026-12-24", "2027-11-26"}


def market_is_open(now=None):
    """True only during regular NYSE hours: 9:30-16:00 ET, Mon-Fri, non-holiday.
    Handles DST automatically via America/New_York."""
    now = now or dt.datetime.now(ET)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    day = now.strftime("%Y-%m-%d")
    if day in MARKET_HOLIDAYS:
        return False
    minutes = now.hour * 60 + now.minute
    close = 13 * 60 if day in EARLY_CLOSES else 16 * 60
    return 9 * 60 + 30 <= minutes < close


def fetch_closes():
    key = os.environ["TWELVEDATA_KEY"]
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={SYMBOL}&interval=5min&outputsize=120&apikey={key}"
    )
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    if data.get("status") == "error":
        print(f"API error: {data.get('message')}", file=sys.stderr)
        sys.exit(1)
    values = data["values"]  # newest first
    closes = [float(v["close"]) for v in reversed(values)]  # oldest -> newest
    latest_ts = values[0]["datetime"]
    return closes, latest_ts


def rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def send_email(subject, body):
    sender = os.environ["GMAIL_ADDRESS"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ALERT_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, os.environ["GMAIL_APP_PASS"])
        s.send_message(msg)


def main():
    # Exit BEFORE any API call when the market is closed - costs zero credits.
    if not market_is_open():
        print("Market closed (ET) - skipping, no API call made")
        return

    closes, latest_ts = fetch_closes()
    current = rsi(closes)
    previous = rsi(closes[:-1])
    if current is None or previous is None:
        print("Not enough data")
        return

    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    below = current < RSI_THRESHOLD
    crossed = previous >= RSI_THRESHOLD and below
    already_alerted = state.get("below", False)

    print(f"{SYMBOL} {latest_ts}: RSI(14) prev={previous:.2f} now={current:.2f}")

    if below and (crossed or not already_alerted):
        price = closes[-1]
        send_email(
            f"S&P 500 alert: 5-min RSI crossed below {RSI_THRESHOLD}",
            f"{SYMBOL} 5-min RSI(14) is {current:.2f} (was {previous:.2f}).\n"
            f"Price: ${price:.2f}\n"
            f"Bar time: {latest_ts}\n\n"
            f"Sent by your Personal Assistant GitHub Action.",
        )
        print("ALERT email sent")
    elif below:
        print("Still below threshold - already alerted, no email")
    else:
        print("Above threshold - no alert")

    STATE_FILE.write_text(json.dumps({"below": below}))


if __name__ == "__main__":
    main()
