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
import io
import json
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

BB_PERIOD = 20
BB_STD = 2
MIN_MARKET_CAP = 20e9
OUT_FILE = Path("data/screener.json")
ET = ZoneInfo("America/New_York")

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"
# Wikipedia 403s requests without a browser-like User-Agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-assistant-screener/1.0)"}


def fetch_tables(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    return pd.read_html(io.StringIO(html))


def get_universe():
    """S&P 500 + Nasdaq-100 tickers from Wikipedia (yfinance format: . -> -)."""
    symbols = set()
    try:
        for tbl in fetch_tables(WIKI_SP500):
            if "Symbol" in tbl.columns:
                symbols.update(tbl["Symbol"].astype(str))
                break
    except Exception as e:
        print(f"WARN: S&P 500 fetch failed: {e}", file=sys.stderr)
    try:
        for tbl in fetch_tables(WIKI_NDX):
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


def rsi_series(closes: pd.Series, period=14):
    """Wilder's RSI on a daily close series; returns the full series (NaN-padded
    at the start) so callers can look back for divergence, not just the latest value."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0, 100.0)
    out = out.astype(float)
    return out


def rsi(closes: pd.Series, period=14):
    """Latest RSI value, or None if there isn't enough history."""
    s = rsi_series(closes, period).dropna()
    return float(s.iloc[-1]) if len(s) else None


def detect_bullish_divergence(closes: pd.Series, period=14, lookback=15):
    """Simplified bullish RSI divergence: today's close is at/near a lookback-window
    low, but today's RSI is meaningfully higher than the RSI on the day that set the
    prior lowest close in that window. Signals selling pressure fading even as price
    probes new lows - a classic early-reversal tell."""
    if len(closes) < period + lookback + 1:
        return False
    rsi_s = rsi_series(closes, period)
    window_closes = closes.iloc[-lookback:]
    window_rsi = rsi_s.iloc[-lookback:]
    today_close = closes.iloc[-1]
    today_rsi = rsi_s.iloc[-1]
    if pd.isna(today_rsi):
        return False
    prior = window_closes.iloc[:-1]
    prior_rsi = window_rsi.iloc[:-1]
    if prior.empty or pd.isna(prior_rsi.min()):
        return False
    prior_low_idx = prior.idxmin()
    prior_low_close = prior.loc[prior_low_idx]
    prior_low_rsi = prior_rsi.loc[prior_low_idx]
    if pd.isna(prior_low_rsi):
        return False
    price_at_or_below = today_close <= prior_low_close * 1.005  # within 0.5%
    rsi_higher = today_rsi > prior_low_rsi + 3  # meaningfully higher, not noise
    return bool(price_at_or_below and rsi_higher)


def macd_signal(closes: pd.Series, fast=12, slow=26, signal=9):
    """Returns (histogram_latest, turning_up) where turning_up means the MACD
    histogram increased for the last 2 bars and is climbing toward/through zero -
    momentum shifting from selling to buying."""
    if len(closes) < slow + signal + 2:
        return None, False
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = (macd_line - signal_line).dropna()
    if len(hist) < 3:
        return None, False
    turning_up = hist.iloc[-1] > hist.iloc[-2] > hist.iloc[-3]
    return float(hist.iloc[-1]), bool(turning_up)


def volume_ratio(volumes: pd.Series, period=20):
    """Today's volume vs the trailing N-day average (excluding today)."""
    v = volumes.dropna()
    if len(v) < period + 1:
        return None
    avg = v.iloc[-(period + 1):-1].mean()
    if not avg:
        return None
    return float(v.iloc[-1] / avg)


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


def get_earnings_surprise(tkr, today):
    """Most recently REPORTED quarter's actual EPS vs analyst estimate."""
    try:
        ed = tkr.get_earnings_dates(limit=8)
        if ed is None or ed.empty:
            return None
        ed = ed.sort_index()
        past = ed[ed.index.date <= today]
        if past.empty:
            return None
        # walk backward to the most recent row that actually has a reported figure
        for date_idx, row in past.iloc[::-1].iterrows():
            actual = row.get("Reported EPS")
            estimate = row.get("EPS Estimate")
            if pd.notna(actual) and pd.notna(estimate):
                # computed directly from actual/estimate rather than trusting
                # yfinance's raw Surprise(%) column, which has an inconsistent
                # fraction-vs-percent format across symbols.
                surprise_pct = ((actual - estimate) / abs(estimate) * 100) if estimate else None
                return {
                    "date": date_idx.date().isoformat(),
                    "actual_eps": float(actual),
                    "estimate_eps": float(estimate),
                    "surprise_pct": float(surprise_pct) if surprise_pct is not None else None,
                }
        return None
    except Exception as e:
        print(f"  earnings surprise failed: {e}", file=sys.stderr)
        return None


def get_social_sentiment(sym):
    """% bullish vs bearish from recent StockTwits posts for this symbol.
    Public, unauthenticated endpoint - no API key. Best-effort: StockTwits
    is not a guaranteed-stable API, so failures just omit sentiment rather
    than breaking the whole screener run."""
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        messages = data.get("messages", []) or []
        bullish = bearish = 0
        for m in messages:
            label = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            if label == "Bullish":
                bullish += 1
            elif label == "Bearish":
                bearish += 1
        total = bullish + bearish
        if total == 0:
            return {"label": "No sentiment data", "bullish_pct": None, "sample_size": len(messages)}
        bullish_pct = round(bullish / total * 100, 1)
        label = "Bullish" if bullish_pct >= 60 else "Bearish" if bullish_pct <= 40 else "Mixed"
        return {"label": label, "bullish_pct": bullish_pct, "sample_size": total}
    except Exception as e:
        print(f"  sentiment failed for {sym}: {e}", file=sys.stderr)
        return None


def get_valuation(tkr, close):
    """Forward/trailing P/E, PEG ratio, and analyst target upside - all from
    yfinance's info dict, which is frequently incomplete, so every field is
    independently optional."""
    try:
        info = tkr.info or {}
    except Exception:
        info = {}
    forward_pe = info.get("forwardPE")
    trailing_pe = info.get("trailingPE")
    peg = info.get("pegRatio") or info.get("trailingPegRatio")
    target_mean = info.get("targetMeanPrice")
    upside_pct = pct(target_mean, close) if target_mean else None
    return {
        "forward_pe": float(forward_pe) if forward_pe else None,
        "trailing_pe": float(trailing_pe) if trailing_pe else None,
        "peg_ratio": float(peg) if peg else None,
        "analyst_target": float(target_mean) if target_mean else None,
        "analyst_upside_pct": upside_pct,
    }


def get_short_interest(tkr):
    try:
        info = tkr.info or {}
    except Exception:
        info = {}
    pct_float = info.get("shortPercentOfFloat")
    days_to_cover = info.get("shortRatio")
    return {
        "pct_of_float": round(float(pct_float) * 100, 2) if pct_float else None,
        "days_to_cover": float(days_to_cover) if days_to_cover else None,
    }


def get_insider_buying(tkr, today, lookback_days=90):
    """Recent (last ~90d) insider purchases from Form 4 filings. yfinance's
    coverage is inconsistent across tickers - treat any failure/empty result
    as 'no data' rather than 'no buying'."""
    try:
        df = tkr.insider_transactions
        if df is None or df.empty:
            return None
        cutoff = today - dt.timedelta(days=lookback_days)
        date_col = "Start Date" if "Start Date" in df.columns else None
        text_col = "Transaction" if "Transaction" in df.columns else None
        if not date_col or not text_col:
            return None
        df = df.copy()
        df["_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
        recent = df[(df["_date"].notna()) & (df["_date"] >= cutoff)]
        buys = recent[recent[text_col].astype(str).str.contains("Purchase", case=False, na=False)]
        if buys.empty:
            return {"recent_buys": 0, "shares": None}
        shares = None
        if "Shares" in buys.columns:
            shares = float(pd.to_numeric(buys["Shares"], errors="coerce").sum())
        return {"recent_buys": int(len(buys)), "shares": shares}
    except Exception as e:
        print(f"  insider lookup failed: {e}", file=sys.stderr)
        return None


def compute_hot_score(valuation, rsi_val, divergence, macd_turning_up, vol_ratio,
                       short_info, insider, sentiment, earnings_surprise):
    """0-100 composite: is this stock not just beaten-down, but actually cheap
    AND showing signs the selling is exhausting AND has a reason to move today.
    Four 25-point buckets; missing data drops out of its own bucket's average
    rather than counting against the stock."""

    def clamp(x, lo=0, hi=25):
        return max(lo, min(hi, x))

    # --- Valuation (cheap relative to growth/expectations) ---
    val_scores = []
    if valuation.get("peg_ratio") is not None:
        peg = valuation["peg_ratio"]
        val_scores.append(clamp(25 * (1 - (peg - 0.5) / 2.5)) if peg > 0 else 0)
    if valuation.get("analyst_upside_pct") is not None:
        up = valuation["analyst_upside_pct"]
        val_scores.append(clamp(25 * (up / 30)))
    if valuation.get("forward_pe") is not None and valuation.get("trailing_pe"):
        improving = valuation["forward_pe"] < valuation["trailing_pe"]
        val_scores.append(18 if improving else 8)
    valuation_score = sum(val_scores) / len(val_scores) if val_scores else 12.5

    # --- Reversal technicals (is the selling actually stopping) ---
    tech_scores = []
    if rsi_val is not None:
        tech_scores.append(clamp(25 * ((40 - rsi_val) / 25)))  # lower RSI -> more coiled
    tech_scores.append(25 if divergence else 5)
    tech_scores.append(22 if macd_turning_up else 6)
    technical_score = sum(tech_scores) / len(tech_scores) if tech_scores else 12.5

    # --- Catalyst strength (reason for a move today) ---
    cat_scores = []
    if vol_ratio is not None:
        cat_scores.append(clamp(25 * ((vol_ratio - 0.8) / 1.7)))
    if short_info and short_info.get("pct_of_float") is not None:
        cat_scores.append(clamp(25 * (short_info["pct_of_float"] / 20)))
    if insider and insider.get("recent_buys"):
        cat_scores.append(23)
    catalyst_score = sum(cat_scores) / len(cat_scores) if cat_scores else 12.5

    # --- Sentiment / earnings momentum ---
    sent_scores = []
    if sentiment and sentiment.get("bullish_pct") is not None:
        label = sentiment["label"]
        sent_scores.append(23 if label == "Bullish" else 12 if label == "Mixed" else 3)
    if earnings_surprise and earnings_surprise.get("surprise_pct") is not None:
        s = earnings_surprise["surprise_pct"]
        sent_scores.append(clamp(12.5 + s / 2))
    sentiment_score = sum(sent_scores) / len(sent_scores) if sent_scores else 12.5

    total = valuation_score + technical_score + catalyst_score + sentiment_score
    return {
        "total": round(total, 1),
        "valuation": round(valuation_score, 1),
        "technical": round(technical_score, 1),
        "catalyst": round(catalyst_score, 1),
        "sentiment": round(sentiment_score, 1),
    }


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
            volumes = data[sym]["Volume"]
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
        candidates.append((sym, closes, volumes, close, float(lower)))
    print(f"Below lower band: {len(candidates)}")

    results = []
    for sym, closes, volumes, close, lower in candidates:
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
        rsi_val = rsi(closes)
        divergence = detect_bullish_divergence(closes)
        macd_hist, macd_turning_up = macd_signal(closes)
        vol_ratio = volume_ratio(volumes)
        valuation = get_valuation(tkr, close)
        short_info = get_short_interest(tkr)
        insider = get_insider_buying(tkr, today)
        sentiment = get_social_sentiment(sym)
        earnings_surprise = get_earnings_surprise(tkr, today)
        hot_score = compute_hot_score(valuation, rsi_val, divergence, macd_turning_up,
                                       vol_ratio, short_info, insider, sentiment,
                                       earnings_surprise)

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
            "rsi": rsi_val,
            "rsi_divergence": divergence,
            "macd_histogram": macd_hist,
            "macd_turning_up": macd_turning_up,
            "volume_ratio": vol_ratio,
            "valuation": valuation,
            "short_interest": short_info,
            "insider": insider,
            "hot_score": hot_score,
            "reasons": build_reasons(sym, closes, chg_1d, chg_5d, chg_1mo, off_52w,
                                     earnings_date, today),
            "news": get_news(tkr),
            "sentiment": sentiment,
            "earnings_surprise": earnings_surprise,
        })
        print(f"  {sym}: ${close:.2f}, {results[-1]['pct_below_band']:.1f}% below band, "
              f"cap ${mc/1e9:.0f}B, hot={hot_score['total']}")

    results.sort(key=lambda r: r["hot_score"]["total"], reverse=True)
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
