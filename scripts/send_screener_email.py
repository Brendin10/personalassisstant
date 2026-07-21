"""
Daily 8:30am ET digest email of the Bollinger Band screener results.
Reads data/screener.json (written the previous evening by
scripts/screen_bollinger.py) and emails the list of oversold stocks with
their probable reasons, plus a link back to the Stocks page.

Run by .github/workflows/screener-email.yml.

Required environment variables (reuses the same secrets as check_rsi.py):
  GMAIL_ADDRESS   - Gmail address to send FROM
  GMAIL_APP_PASS  - Gmail app password (myaccount.google.com/apppasswords)
  PAGES_URL       - set by the workflow from the repo's GitHub Pages URL
"""

import datetime as dt
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

ALERT_TO = "Brendunvnz@gmail.com"
DATA_FILE = Path("data/screener.json")
STATE_FILE = Path(".email_state.json")  # dedupe: only one send per calendar day
ET = ZoneInfo("America/New_York")

# Cron fires several times inside a window around 8:30am ET (to survive the
# EDT/EST shift); only actually send within a few minutes of 8:30.
TARGET_MINUTES = 8 * 60 + 30
WINDOW_MINUTES = 6


def in_send_window(now=None):
    now = now or dt.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return abs(minutes - TARGET_MINUTES) <= WINDOW_MINUTES


def already_sent_today(today_str):
    if not STATE_FILE.exists():
        return False
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return False
    return state.get("last_sent") == today_str


def mark_sent(today_str):
    STATE_FILE.write_text(json.dumps({"last_sent": today_str}))


def fmt_pct(v, digits=1):
    return f"{v:+.{digits}f}%" if v is not None else "n/a"


def build_bodies(payload, pages_url):
    results = payload.get("results", [])
    updated = payload.get("updated", "")
    stocks_url = pages_url.rstrip("/") + "/#stocks"

    if not results:
        text = (
            f"No stocks closed below their lower Bollinger Band with a market cap "
            f"over $20B as of {updated}.\n\n"
            f"View the screener: {stocks_url}\n"
        )
        html = (
            f"<p>No stocks closed below their lower Bollinger Band with a market cap "
            f"over $20B as of {updated}.</p>"
            f'<p><a href="{stocks_url}">View the screener</a></p>'
        )
        return text, html

    text_lines = [f"Oversold screener — {updated}", f"{len(results)} stock(s) below their lower Bollinger Band, market cap > $20B:\n"]
    html_rows = []
    for r in results:
        cap_b = r["market_cap"] / 1e9
        reason = (r.get("reasons") or ["No reason available."])[0]
        text_lines.append(
            f"{r['symbol']} ({r.get('name') or r['symbol']}) — ${r['close']:.2f}, "
            f"{r['pct_below_band']:.1f}% below band, cap ${cap_b:.0f}B, "
            f"1d {fmt_pct(r.get('chg_1d'))}\n  Reason: {reason}\n"
        )
        html_rows.append(
            "<tr>"
            f'<td style="padding:6px 10px;font-weight:bold">{r["symbol"]}</td>'
            f'<td style="padding:6px 10px">{r.get("name") or ""}</td>'
            f'<td style="padding:6px 10px">${r["close"]:.2f}</td>'
            f'<td style="padding:6px 10px;color:#b00">{r["pct_below_band"]:.1f}% below band</td>'
            f'<td style="padding:6px 10px">${cap_b:.0f}B</td>'
            f'<td style="padding:6px 10px">{fmt_pct(r.get("chg_1d"))}</td>'
            "</tr>"
            f'<tr><td></td><td colspan="5" style="padding:0 10px 12px;color:#555">{reason}</td></tr>'
        )

    text_lines.append(f"\nView the full screener: {stocks_url}\n")
    text = "\n".join(text_lines)

    html = f"""
    <div style="font-family:Arial,Helvetica,sans-serif">
      <h2 style="margin-bottom:4px">Oversold screener</h2>
      <p style="color:#555;margin-top:0">{updated} — {len(results)} stock(s) below their
      lower Bollinger Band (20, 2σ), market cap over $20B.</p>
      <table style="border-collapse:collapse;width:100%">
        <thead>
          <tr style="text-align:left;border-bottom:2px solid #ccc">
            <th style="padding:6px 10px">Symbol</th><th style="padding:6px 10px">Name</th>
            <th style="padding:6px 10px">Close</th><th style="padding:6px 10px">Below band</th>
            <th style="padding:6px 10px">Mkt cap</th><th style="padding:6px 10px">1d chg</th>
          </tr>
        </thead>
        <tbody>{"".join(html_rows)}</tbody>
      </table>
      <p style="margin-top:20px">
        <a href="{stocks_url}" style="background:#ffc75f;color:#111;padding:10px 18px;
        text-decoration:none;border-radius:6px;font-weight:bold">Open the Stocks page</a>
      </p>
    </div>
    """
    return text, html


def send_email(subject, text_body, html_body):
    sender = os.environ["GMAIL_ADDRESS"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ALERT_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, os.environ["GMAIL_APP_PASS"])
        s.send_message(msg)


def main():
    now = dt.datetime.now(ET)
    today_str = now.strftime("%Y-%m-%d")

    if not in_send_window(now):
        print(f"Not in send window ({now:%H:%M} ET) - skipping")
        return
    if already_sent_today(today_str):
        print(f"Already sent today ({today_str}) - skipping")
        return
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} not found - screener hasn't run yet", file=sys.stderr)
        return

    payload = json.loads(DATA_FILE.read_text())
    pages_url = os.environ.get("PAGES_URL", "").strip()
    text_body, html_body = build_bodies(payload, pages_url)

    n = len(payload.get("results", []))
    subject = (
        f"Oversold screener: {n} stock{'s' if n != 1 else ''} below Bollinger Band (>$20B)"
        if n else "Oversold screener: no hits today"
    )
    send_email(subject, text_body, html_body)
    mark_sent(today_str)
    print(f"Email sent to {ALERT_TO} ({n} stocks)")


if __name__ == "__main__":
    main()
