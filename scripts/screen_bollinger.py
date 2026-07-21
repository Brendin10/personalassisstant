"""
Nightly Bollinger Band screener.
Finds stocks that closed BELOW their lower Bollinger Band (20-day, 2 std dev)
with a market cap over $20B, and writes data/screener.json for the web page.

Universe: S&P 500 + Nasdaq-100 (covers nearly all US-listed stocks > $20B).
Data: free via yfinance. Run by .github/workflows/bollinger-screen.yml.

For each hit it also gathers "probable reason" insights: price-action
heuristics (earnings-driven? sharp one-day drop? steady slide?) plus
recent news headlines.
"""

import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

BB_PERIOD = 20
BB_STD = 2
MIN_MARKET_CAP = 20e9
OUT_FILE = Path("data/screener.json")
ET = ZoneInfo("America/New_York")

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"


def get_universe():
    """S&P 500 + Nasdaq-100 tickers from Wikipedia (yfinance format: . -> -)."""
    symbols = set()
    try:
        for tbl in pd.read_html(WIKI_SP500):
            if "Symbol" in tbl.columns:
                symbols.update(tbl["Symbol"].astype(str))
                break
    except Exception as e:
        print(f"WARN: S&P 500 fetch failed: {e}", file=sys.stderr)
    try:
        for tbl in pd.read_html(WIKI_NDX):
            for col in ("Ticker", "Symbol"):
                if col in tbl.columns:
                    symbols.update(tbl[col].astype(str))
                    break
    except Exception as e:
        print(f"WARN: Nasdaq-100 fetch failed: {e}", file=sys.stderr)
    symbols = {s.strip().replace(".", "-") for s in symbols if s and s.strip().isascii()}
    symbols = {s for s in symbols if s.replace("-", "").isalnum() and len(s) <= 6}
    if len(symbols) < 400:
        print(f"ERROR: universe too small ({len(symbols)}), aborting", file=sys.stderr)
        sys.exit(1)
    return sorted(symbols)


def rsi(closes: pd.Series, period=14):
    """Wilder's RSI on a daily close series; returns latest value."""
    delta = closes.diff().dropna()
    if len(delta) < period + 1:
        return None
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    if avg_loss.iloc[-1] == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1]
    return float(100 - 100 / (1 + rs))


def pct(a, b):
    """Percent change from b to a."""
    if b is None or b == 0 or pd.isna(b) or pd.isna(a):
        return None
    return float((a / b - 1) * 100)


def get_news(tkr):
    """Top 3 recent headlines; handles both old and new yfinance news shapes."""
    items = []
    try:
        for n in (tkr.news or [])[:3]:
            c = n.get("content", n)  # new shape nests under "content"
            title = c.get("title")
            link = (
                (c.get("clickThroughUrl") or {}).get("url")
                or (c.get("canonicalUrl") or {}).get("url")
                or n.get("link")
            )
            publisher = (c.get("provider") or {}).get("displayName") or n.get("publisher")
            if title and link:
                items.append({"title": title, "link": link, "publisher": publisher})
    except Exception as e:
        print(f"  news failed: {e}", file=sys.stderr)
    return items


def get_earnings_date(tkr, today):
    """Most relevant earnings date within +/-14 days, else None."""
    try:
        ed = tkr.get_earnings_dates(limit=8)
        if ed is None or ed.empty:
            return None
        dates = [d.date() for d in ed.index]
        near = [d for d in dates if abs((d - today).days) <= 14]
        if not near:
            return None
        return min(near, key=lambda d: abs((d - today).days))
    except Exception:
        return None


def build_reasons(sym, closes, chg_1d, chg_5d, chg_1mo, off_52w, earnings_date, today):
    reasons = []
    if earnings_date is not None:
        when = "reported earnings" if earnings_date <= today else "reports earnings"
        reasons.append(
            f"{sym} {when} on {earnings_date:%b %d} — earnings reactions are the most "
            "common cause of a sharp break below the band."
        )
    # biggest single-day drop in the last 5 sessions
    recent = closes.pct_change().iloc[-5:] * 100
    worst = recent.min()
    if pd.notna(worst) and worst <= -5:
        worst_day = recent.idxmin()
        reasons.append(
            f"Sharp one-day drop of {worst:.1f}% on {worst_day:%b %d} — points to a "
            "specific catalyst (news, guidance, downgrade) rather than gradual selling."
        )
    elif chg_5d is not None and chg_5d <= -8:
        reasons.append(
            f"Down {chg_5d:.1f}% over 5 sessions without a single large gap — suggests "
            "sustained selling pressure (sector rotation, macro, or sentiment shift)."
        )
    if chg_1mo is not None and chg_1mo <= -15:
        reasons.append(
            f"Down {chg_1mo:.1f}% over the past month — an extended downtrend; the band "
            "break continues an existing move rather than starting one."
        )
    if off_52w is not None and off_52w <= -40:
        reasons.append(
            f"Trading {abs(off_52w):.0f}% below its 52-week high — deep drawdown territory, "
            "often reflecting a broken growth story or re-rating."
        )
    if not reasons:
        reasons.append(
            "No single obvious catalyst in the price action — likely broad market or "
            "sector weakness. Check the headlines below."
        )
    return reasons


def main():
    today = dt.datetime.now(ET).date()
    universe = get_universe()
    print(f"Universe: {len(universe)} symbols")

    data = yf.download(
        universe, period="1y", interval="1d", group_by="ticker",
        threads=True, progress=False, auto_adjust=False,
    )

    candidates = []
    for sym in universe:
        try:
            closes = data[sym]["Close"].dropna()
        except KeyError:
            continue
        if len(closes) < BB_PERIOD + 5:
            continue
        mid = closes.rolling(BB_PERIOD).mean().iloc[-1]
        std = closes.rolling(BB_PERIOD).std(ddof=0).iloc[-1]
        lower = mid - BB_STD * std
        close = float(closes.iloc[-1])
        if pd.isna(lower) or close >= lower:
            continue
        candidates.append((sym, closes, close, float(lower)))
    print(f"Below lower band: {len(candidates)}")

    results = []
    for sym, closes, close, lower in candidates:
        tkr = yf.Ticker(sym)
        try:
            mc = tkr.fast_info.get("market_cap") or tkr.info.get("marketCap")
        except Exception:
            mc = None
        if not mc or mc < MIN_MARKET_CAP:
            continue
        try:
            name = tkr.info.get("shortName") or ""
        except Exception:
            name = ""

        chg_1d = pct(closes.iloc[-1], closes.iloc[-2]) if len(closes) >= 2 else None
        chg_5d = pct(closes.iloc[-1], closes.iloc[-6]) if len(closes) >= 6 else None
        chg_1mo = pct(closes.iloc[-1], closes.iloc[-22]) if len(closes) >= 22 else None
        off_52w = pct(close, float(closes.max()))
        earnings_date = get_earnings_date(tkr, today)

        results.append({
            "symbol": sym,
            "name": name,
            "close": close,
            "lower_band": lower,
            "pct_below_band": float((lower - close) / lower * 100),
            "market_cap": float(mc),
            "chg_1d": chg_1d,
            "chg_5d": chg_5d,
            "chg_1mo": chg_1mo,
            "off_52w_high": off_52w,
            "rsi": rsi(closes),
            "reasons": build_reasons(sym, closes, chg_1d, chg_5d, chg_1mo, off_52w,
                                     earnings_date, today),
            "news": get_news(tkr),
        })
        print(f"  {sym}: ${close:.2f}, {results[-1]['pct_below_band']:.1f}% below band, "
              f"cap ${mc/1e9:.0f}B")

    results.sort(key=lambda r: r["pct_below_band"], reverse=True)
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "updated": dt.datetime.now(ET).strftime("%b %d, %Y %I:%M %p ET"),
        "criteria": f"close < lower BB({BB_PERIOD},{BB_STD}), market cap > ${MIN_MARKET_CAP/1e9:.0f}B",
        "universe_size": len(universe),
        "results": results,
    }, indent=1))
    print(f"Wrote {OUT_FILE} with {len(results)} stocks")


if __name__ == "__main__":
    main()
