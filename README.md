# Personal Assistant

A personal assistant agent hosted on GitHub Pages. Landing page with four modes: **Personal**, **Stocks**, **Quantum**, and **Creative**.

## Deploy to GitHub Pages

1. Create a new repository on [github.com/new](https://github.com/new) (e.g. `personal-assistant`). Set it to **Public**.
2. Push this folder to it:

   ```bash
   cd "C:\Users\Owner\Personal Assistant"
   git init
   git add .
   git commit -m "Initial landing page"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/personal-assistant.git
   git push -u origin main
   ```

3. In the repo: **Settings → Pages → Source: Deploy from a branch → Branch: main / (root) → Save**.
4. After a minute, the site is live at `https://YOUR_USERNAME.github.io/personal-assistant/`.

## Structure

- `index.html` — the entire site (HTML, CSS, and JS in one file). Each menu option opens a placeholder section, ready to build out.

## Email alerts (GitHub Action)

`.github/workflows/rsi-alert.yml` checks the S&P 500 (SPY) 5-min RSI(14) every ~5 minutes during US market hours and emails **Brendunvnz@gmail.com** when RSI crosses below 30. It only sends one email per oversold episode (no spam while RSI stays low).

Setup (once, after pushing to GitHub):

1. Get a free API key at [twelvedata.com](https://twelvedata.com).
2. Create a Gmail **app password** for the sending account: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-step verification enabled).
3. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add all three:
   - `TWELVEDATA_KEY` — your Twelve Data API key
   - `GMAIL_ADDRESS` — the Gmail address to send from (e.g. forecastplusllc@gmail.com)
   - `GMAIL_APP_PASS` — the 16-character app password
4. Test it: **Actions tab → S&P 500 RSI Alert → Run workflow**.

**API credit optimization:** both the workflow script and the web page check NYSE hours (9:30 AM–4:00 PM ET, Mon–Fri, skipping holidays and 1 PM early closes, DST-aware) *before* calling the API — outside market hours, zero credits are used. The page polls every 5 minutes (matching the candle interval) and pauses while its tab is hidden. Worst case with both running all day: ~156 calls/trading day, well under Twelve Data's ~800/day free tier. The holiday lists in `scripts/check_rsi.py` and `index.html` cover 2026–2027; update them yearly.

Notes: GitHub's scheduler is best-effort, so checks can lag a few minutes. Scheduled workflows are auto-disabled after 60 days of no repo activity — an occasional commit keeps them alive.

## Stocks: watchlist

The Stocks page has a personal watchlist (add/remove tickers, saved in your browser via localStorage). Quotes come from Twelve Data `/quote` (1 credit per symbol) using the same API key as the alerts. Credit-aware: refreshes every 5 min only while the market is open and the tab is visible, plus one fetch on page load so you see last close after hours. Worst case with 8 tickers: ~624 extra credits/trading day — keep the watchlist small (≤5 tickers recommended) to stay comfortably under the ~800/day free tier alongside the RSI checks.

## Stocks: Bollinger Band screener

A second, automatic watchlist: every weekday at ~5:45 PM ET, `.github/workflows/bollinger-screen.yml` runs `scripts/screen_bollinger.py`, which screens the S&P 500 + Nasdaq-100 (covers nearly all US-listed stocks over $20B) for stocks that **closed below their lower Bollinger Band (20-day, 2σ)** with a **market cap over $20B**. Results are committed to `data/screener.json`, which the Stocks page displays — zero Twelve Data credits, no API key needed.

Click any row for a popup with insights: how far below the band, 1d/5d/1mo moves, RSI, distance from the 52-week high, **probable reasons for the drop** (earnings timing, sharp one-day catalyst vs. gradual slide, extended drawdown), and recent news headlines.

Notes:
- Data comes from Yahoo Finance via `yfinance` (free, no key). No repo secrets needed — but the workflow needs no setup either; it just runs after you push.
- The screener list appears empty until the Action has run at least once (trigger it manually: **Actions → Bollinger Band Screener → Run workflow**).
- Insights are price-action heuristics plus headlines — a starting point for research, not financial advice.

### Email digest

`.github/workflows/screener-email.yml` runs `scripts/send_screener_email.py` every weekday ~8:30 AM ET and emails the current screener list (symbol, price, % below band, market cap, 1-day move, and the top probable reason for each) to **Brendunvnz@gmail.com**, with a button linking straight to the Stocks page (`#stocks`) on your GitHub Pages site. If nothing currently qualifies, it still sends a short "no hits today" email so you know it ran.

- Reuses the same `GMAIL_ADDRESS` / `GMAIL_APP_PASS` secrets as the RSI alert — no additional setup needed once those are set.
- The page link is built automatically from the repo owner/name (`https://<owner>.github.io/<repo>/`), so it works as soon as you push and enable Pages.
- Like the RSI workflow, the cron fires several times in a window around 8:30 AM to survive the EDT/EST shift; the script checks the real Eastern time and a per-day dedupe file so only one email goes out.
- It reads whatever `data/screener.json` currently holds, which is the *previous trading day's* close (written by the Bollinger screener ~5:45 PM ET the evening before).

## Next steps

- Personal: task list and notes (can persist with localStorage on GitHub Pages)
- Stocks: portfolio tracking
- Quantum: in-browser circuit simulator
- Creative: project board and idea vault
