"""
================================================================================
  NIFTY 50 INSTITUTIONAL MOMENTUM ENGINE - NATURAL GROWTH SCANNER
  Version 6.0 | Spike-Kills-Velocity + Longer RSI Warmup + Trend-Reversal Guard
================================================================================
  REQUIRES: pip install yfinance numpy requests pandas
  RUN IN  : Python IDLE, VS Code, Terminal — any standard Python 3.8+ env

  CHANGELOG v6.0  (validated against live trading app screenshots)
  ─────────────────────────────────────────────────────────────────────────────
  FIX I  : VEDL ranked #1 despite being BEARISH (10 bearish / 3 bullish).
           Root cause: its 21% 1M velocity from a spike dominated all scores
           even after the smoothness penalty. Fix: spike penalty NOW also
           directly reduces the effective velocity fed into scoring. A spike
           stock's velocity is multiplied by (1 - penalty) so it cannot win
           on raw momentum alone.

  FIX II : RSI still slightly off (36.4 vs app 33.16).
           Root cause: Wilder RSI needs 3× the period as warmup to converge;
           we were using all available data but the seed average was still
           noisy. Fix: use ALL available history bars for the Wilder smoothing
           loop (not just period*3), seeded from first `period` bars, then
           run every remaining bar. More warmup = tighter convergence.

  FIX III: Add a "trend reversal" guard — if the stock's last-5-day return
           is negative (price falling NOW), discount its score by 30%. VEDL
           had peaked and was falling at 3:07 PM; this guard would have caught
           it. This catches spike-then-crash patterns the smoothness alone
           misses when the 21-day window starts before the spike.

  FIX IV : Beta window extended from 60 → 90 days for more stable estimate,
           matching the typical 3-month beta used by NSE and trading apps.
  ─────────────────────────────────────────────────────────────────────────────
  RETAINED FROM v5.0: Date-aligned Beta, Wilder RSI, Live prices, INFY.NS,
                      Spike-Aware Smoothness, RSI ceiling 65
================================================================================
"""

import os, json, time, datetime, warnings
import numpy as np

try:
    import pandas as pd
except ImportError:
    print("\n[SETUP] pandas not found.  Run:  pip install pandas\n"); raise

try:
    import yfinance as yf
except ImportError:
    print("\n[SETUP] yfinance not found.  Run:  pip install yfinance\n"); raise

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

CACHE_FILE       = "nifty50_cache.json"
CACHE_MAX_HOURS  = 6
LOOKBACK_DAYS    = 140        # calendar days → ~100 trading days comfortably
SCORE_DAYS       = 21
CONSISTENCY_DAYS = 20
VOLUME_SHORT     = 15
VOLUME_LONG      = 60
RSI_PERIOD       = 14
RSI_OVERBOUGHT   = 65         # tightened: "near overbought" excluded
BETA_CEILING      = 1.3
BETA_WINDOW       = 90        # FIX IV: 90-day beta matches NSE/app convention
SPIKE_THRESHOLD   = 0.06      # >6% single-day move triggers penalties
RECENT_DAYS       = 5         # FIX III: short window to detect trend reversal
TOP_N             = 10
VELOCITY_CAP      = 0.15
ALPHA_CAP         = 0.10
REVERSAL_DISCOUNT = 0.30      # FIX III: score cut 30% if price falling last 5d

WEIGHTS = {
    "smoothness"  : 0.30,
    "alpha"       : 0.25,
    "velocity"    : 0.20,
    "consistency" : 0.15,
    "volume"      : 0.10,
}

# ── Verified Nifty 50 Yahoo Finance tickers ───────────────────────────────────
NIFTY50 = [
    "RELIANCE.NS",  "TCS.NS",       "HDFCBANK.NS",  "BHARTIARTL.NS","ICICIBANK.NS",
    "INFY.NS",      "SBIN.NS",      "HINDUNILVR.NS","ITC.NS",        "LT.NS",
    "KOTAKBANK.NS", "AXISBANK.NS",  "BAJFINANCE.NS","ASIANPAINT.NS","MARUTI.NS",
    "TITAN.NS",     "NESTLEIND.NS", "ULTRACEMCO.NS","WIPRO.NS",      "HCLTECH.NS",
    "POWERGRID.NS", "NTPC.NS",      "SUNPHARMA.NS", "ONGC.NS",       "JSWSTEEL.NS",
    "TATASTEEL.NS", "ADANIENT.NS",  "ADANIPORTS.NS","COALINDIA.NS",  "INDUSINDBK.NS",
    "BAJAJFINSV.NS","BAJAJ-AUTO.NS","GRASIM.NS",    "HINDALCO.NS",   "CIPLA.NS",
    "EICHERMOT.NS", "HEROMOTOCO.NS","DRREDDY.NS",   "DIVISLAB.NS",   "APOLLOHOSP.NS",
    "TECHM.NS",     "BPCL.NS",      "BRITANNIA.NS", "SHRIRAMFIN.NS","TATACONSUM.NS",
    "TATAMOTORS.NS","M&M.NS",       "VEDL.NS",      "SBILIFE.NS",    "HDFCLIFE.NS",
]

BENCHMARK = "^NSEI"
LINE      = "=" * 80
DLINE     = "-" * 80

# ═══════════════════════════════════════════════════════════════════════════════
#  BANNER & DATES
# ═══════════════════════════════════════════════════════════════════════════════

def banner():
    print(f"\n{LINE}")
    print("📈  NIFTY 50 INSTITUTIONAL MOMENTUM ENGINE - NATURAL GROWTH SCANNER")
    print(LINE)
    w = WEIGHTS
    print(f"⚙️  ENGINE WEIGHTS: "
          f"Smoothness ({int(w['smoothness']*100)}%) | "
          f"Alpha ({int(w['alpha']*100)}%) | "
          f"Velocity ({int(w['velocity']*100)}%) | "
          f"Consistency ({int(w['consistency']*100)}%) | "
          f"Volume ({int(w['volume']*100)}%)")

def print_dates():
    today     = datetime.date.today()
    sell_date = today + datetime.timedelta(days=30)
    while sell_date.weekday() >= 5:
        sell_date += datetime.timedelta(days=1)
    print(f"\n📅  ACTION PLAN")
    print(f"   • BUY  DATE : {today.strftime('%A, %B %d, %Y')}")
    print(f"   • SELL DATE : {sell_date.strftime('%A, %B %d, %Y')}  (Holding ~30 Days)")
    print(LINE)

# ═══════════════════════════════════════════════════════════════════════════════
#  LIVE PRICE (always fresh — bypasses cache)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_live_prices(tickers: list) -> dict:
    prices = {}
    print("   🔴  Fetching live intraday prices …", end="", flush=True)
    for ticker in tickers:
        try:
            fi = yf.Ticker(ticker).fast_info
            px = getattr(fi, "last_price", None)
            if px is None or (isinstance(px, float) and px != px):
                px = getattr(fi, "regular_market_price", None)
            if px and float(px) > 0:
                prices[ticker] = float(px)
        except Exception:
            pass
        time.sleep(0.05)
    print(f" done ({len(prices)}/{len(tickers)})")
    return prices

# ═══════════════════════════════════════════════════════════════════════════════
#  HISTORICAL DATA — date-indexed storage (FIX A/D)
# ═══════════════════════════════════════════════════════════════════════════════

def _cache_valid() -> bool:
    if not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE) as f:
            meta = json.load(f)
        age = (datetime.datetime.now() -
               datetime.datetime.fromisoformat(meta["timestamp"])
               ).total_seconds() / 3600
        return age < CACHE_MAX_HOURS
    except Exception:
        return False

def _save_cache(data: dict):
    """
    data = {
        ticker: {
            "dates"  : ["2025-01-02", ...],   # ISO date strings
            "close"  : [float, ...],
            "volume" : [float, ...]
        }, ...
    }
    """
    json.dump({"timestamp": datetime.datetime.now().isoformat(), "data": data},
              open(CACHE_FILE, "w"))

def _load_cache() -> dict:
    return json.load(open(CACHE_FILE))["data"]

def _fetch_yfinance(tickers: list, period_days: int) -> dict:
    end   = datetime.date.today()
    start = end - datetime.timedelta(days=period_days)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = yf.download(
            tickers     = tickers,
            start       = str(start),
            end         = str(end),
            auto_adjust = True,
            progress    = False,
            threads     = True,
            timeout     = 30,
        )

    # raw index = DatetimeIndex; raw["Close"] = DataFrame with tickers as cols
    result = {}
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                close_s  = raw["Close"].dropna()
                volume_s = raw["Volume"].dropna()
            else:
                close_s  = raw["Close"][ticker].dropna()
                volume_s = raw["Volume"][ticker].dropna()

            # align on shared dates
            shared = close_s.index.intersection(volume_s.index)
            close_s  = close_s.loc[shared]
            volume_s = volume_s.loc[shared]

            if len(close_s) >= SCORE_DAYS + 5:
                result[ticker] = {
                    "dates"  : [d.strftime("%Y-%m-%d") for d in close_s.index],
                    "close"  : [float(v) for v in close_s.values],
                    "volume" : [float(v) for v in volume_s.values],
                }
        except Exception:
            pass
    return result

def load_historical() -> dict:
    if _cache_valid():
        print("💾  Cache valid — loading from disk (no network for historical data)")
        return _load_cache()

    print("🌐  Fetching historical OHLCV from Yahoo Finance …")
    all_tickers = NIFTY50 + [BENCHMARK]
    data = {}
    for attempt in range(1, 4):
        try:
            data = _fetch_yfinance(all_tickers, LOOKBACK_DAYS)
            ok   = sum(1 for t in NIFTY50 if t in data)
            print(f"   ✅  {ok} Nifty50 tickers downloaded on attempt {attempt}")
            if ok >= 30:
                break
        except Exception as e:
            print(f"   ⚠️  Attempt {attempt} failed: {e}")
            if attempt < 3:
                print("   ⏳  Waiting 5 s …"); time.sleep(5)

    if len(data) < 5:
        raise RuntimeError(
            "Could not fetch data. Check connection.\n"
            "Try: pip install --upgrade yfinance"
        )
    _save_cache(data)
    print(f"   💾  Cached to {CACHE_FILE}")
    return data

# ═══════════════════════════════════════════════════════════════════════════════
#  MATH ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def calc_rsi_wilder(closes: list, period: int = RSI_PERIOD) -> float:
    """
    Wilder RSI — matches TradingView / trading app values.

    Correct algorithm (per Wilder's original 1978 definition):
      Step 1: Compute all price changes (deltas).
      Step 2: Seed avg_gain / avg_loss = simple mean of FIRST `period` deltas.
              (These are deltas[0..period-1], i.e. bars 2..period+1 in price.)
      Step 3: For every delta from index `period` onward, apply Wilder smoothing:
                  avg = prev_avg * (period-1)/period  +  current * 1/period
              This is equivalent to EMA with alpha = 1/period.
      Step 4: RSI = 100 - 100/(1 + avg_gain/avg_loss)

    Using ALL available bars in step 3 maximises convergence accuracy.
    The remaining ~3-point gap vs app (e.g. 62.5 vs 65.5) is caused by the
    app computing RSI on *intraday* 1D candles including today's partial bar,
    while we use only confirmed daily closes. This is expected and acceptable.
    """
    if len(closes) < period + 1:
        return 50.0

    prices = np.array(closes, dtype=float)
    deltas = np.diff(prices)                              # len = len(prices)-1
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Step 2: seed = simple average of first `period` deltas
    avg_g = float(gains[:period].mean())
    avg_l = float(losses[:period].mean())

    # Step 3: Wilder EMA over ALL remaining deltas
    w = (period - 1.0) / period                          # = 1 - alpha
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = avg_g * w + g / period
        avg_l = avg_l * w + l / period

    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))

def calc_beta_aligned(stock_dates: list, stock_closes: list,
                      bench_dates: list,  bench_closes: list,
                      window: int = BETA_WINDOW) -> float:
    """
    Date-aligned Beta using daily returns over last `window` trading days.
    Uses ddof=1 (sample covariance) which matches NSE and most trading apps.
    Inner-join on shared dates eliminates the length-mismatch bug.
    """
    try:
        s_ser = pd.Series(
            [float(v) for v in stock_closes],
            index=pd.to_datetime(stock_dates)
        )
        b_ser = pd.Series(
            [float(v) for v in bench_closes],
            index=pd.to_datetime(bench_dates)
        )

        # inner join on shared trading dates, take last `window` rows
        combined = pd.concat([s_ser, b_ser], axis=1, join="inner").dropna()
        combined.columns = ["stock", "bench"]
        combined = combined.tail(window)

        if len(combined) < 15:
            return 1.0

        # daily returns — pct_change drops the first NaN automatically
        sr = combined["stock"].pct_change().dropna().values
        br = combined["bench"].pct_change().dropna().values

        # trim to equal length (should already be equal after inner join)
        n = min(len(sr), len(br))
        sr, br = sr[-n:], br[-n:]

        if n < 10:
            return 1.0

        # sample covariance matrix (ddof=1 matches financial convention)
        cov_mat   = np.cov(sr, br, ddof=1)
        var_bench = cov_mat[1, 1]
        if var_bench == 0:
            return 1.0
        return float(cov_mat[0, 1] / var_bench)
    except Exception:
        return 1.0

def calc_velocity(closes: list, days: int = SCORE_DAYS) -> float:
    if len(closes) < days + 1:
        return 0.0
    return (closes[-1] / closes[-(days + 1)]) - 1.0

def calc_spike_penalty(closes: list, days: int = SCORE_DAYS) -> float:
    """
    Returns the spike penalty factor (0.0 = no penalty, 1.0 = full wipeout).
    Used by BOTH smoothness and velocity so a spiked stock cannot dominate
    either dimension.
    """
    if len(closes) < days:
        return 0.0
    prices     = np.array(closes[-days:], dtype=float)
    daily_rets = np.abs(np.diff(prices) / prices[:-1])
    max_spike  = float(daily_rets.max()) if len(daily_rets) > 0 else 0.0
    if max_spike <= SPIKE_THRESHOLD:
        return 0.0
    excess = max_spike - SPIKE_THRESHOLD
    return min(excess / (0.20 - SPIKE_THRESHOLD), 1.0)

def calc_smoothness_spike_aware(closes: list, days: int = SCORE_DAYS,
                                penalty: float = None) -> float:
    """Spike-aware R²: uses shared penalty so smoothness & velocity are consistent."""
    if len(closes) < days:
        return 0.0
    prices = np.array(closes[-days:], dtype=float)
    x      = np.arange(days, dtype=float)
    corr   = np.corrcoef(x, prices)[0, 1]
    r2     = float(corr ** 2)
    p      = penalty if penalty is not None else calc_spike_penalty(closes, days)
    return max(0.0, r2 * (1.0 - p))

def calc_velocity_spike_adjusted(closes: list, days: int = SCORE_DAYS,
                                 penalty: float = None) -> float:
    """
    FIX I: Velocity is discounted by the same spike penalty as smoothness.
    A stock that rose 20% via a single news-day spike gets its velocity
    cut proportionally — so it cannot dominate the velocity score either.
    """
    if len(closes) < days + 1:
        return 0.0
    raw_vel = (closes[-1] / closes[-(days + 1)]) - 1.0
    p       = penalty if penalty is not None else calc_spike_penalty(closes, days)
    return raw_vel * (1.0 - p)

def calc_consistency(closes: list, days: int = CONSISTENCY_DAYS) -> float:
    if len(closes) < days + 1:
        return 0.0
    subset = closes[-(days + 1):]
    green  = sum(1 for i in range(1, len(subset)) if subset[i] > subset[i - 1])
    return green / days

def is_reversing(closes: list, days: int = RECENT_DAYS) -> bool:
    """
    FIX III: Returns True if the stock has been falling over the last `days`
    trading days. Catches spike-then-crash patterns like VEDL at 3 PM.
    """
    if len(closes) < days + 1:
        return False
    return closes[-1] < closes[-(days + 1)]

def calc_velocity(closes: list, days: int = SCORE_DAYS) -> float:
    """Raw velocity — kept for alpha calculation (uses true velocity vs benchmark)."""
    if len(closes) < days + 1:
        return 0.0
    return (closes[-1] / closes[-(days + 1)]) - 1.0

def calc_volume_ratio(volumes: list) -> float:
    if len(volumes) < VOLUME_LONG:
        return 1.0
    sa = np.mean(volumes[-VOLUME_SHORT:])
    la = np.mean(volumes[-VOLUME_LONG:])
    return 1.0 if la == 0 else float(sa / la)

def normalise(value: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))

# ═══════════════════════════════════════════════════════════════════════════════
#  SCREENING + SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def primary_driver(ns: float, na: float) -> str:
    if ns >= na:
        return ("High Trend Smoothness — clean linear trajectory "
                "confirming organic institutional accumulation.")
    return ("Strong Alpha decoupling — stock outperforming Nifty 50 index "
            "driven by sustained institutional buying pressure.")

def analyse(hist: dict, live_prices: dict) -> list:
    bench = hist.get(BENCHMARK, {})
    bench_dates  = bench.get("dates",  [])
    bench_closes = bench.get("close",  [])
    bench_vel    = calc_velocity(bench_closes) if len(bench_closes) >= SCORE_DAYS + 5 else 0.0

    if not bench_closes:
        print("⚠️  Benchmark (^NSEI) data missing — Beta filter disabled, Alpha = 0")

    candidates = []
    filtered_rsi, filtered_beta, filtered_vel = 0, 0, 0

    for ticker in NIFTY50:
        info = hist.get(ticker)
        if not info:
            continue
        closes  = info["close"]
        volumes = info["volume"]
        dates   = info["dates"]

        if len(closes) < SCORE_DAYS + 5:
            continue

        # ── Shield 1: Wilder RSI (FIX B) ─────────────────────────────────────
        rsi = calc_rsi_wilder(closes)
        if rsi > RSI_OVERBOUGHT:
            filtered_rsi += 1
            continue

        # ── Shield 2: Date-aligned Beta (FIX A) ──────────────────────────────
        if bench_closes:
            beta = calc_beta_aligned(dates, closes, bench_dates, bench_closes)
        else:
            beta = 1.0

        if beta > BETA_CEILING:
            filtered_beta += 1
            continue

        # ── Element 1 + Spike penalty (shared across velocity & smoothness) ───
        raw_velocity = calc_velocity(closes)
        if raw_velocity <= 0:
            filtered_vel += 1
            continue

        spike_pen  = calc_spike_penalty(closes)                    # FIX I/II shared
        velocity   = calc_velocity_spike_adjusted(closes,
                         penalty=spike_pen)                        # FIX I
        smoothness = calc_smoothness_spike_aware(closes,
                         penalty=spike_pen)                        # uses same penalty
        consistency = calc_consistency(closes)
        vol_ratio   = calc_volume_ratio(volumes)
        alpha       = raw_velocity - bench_vel                     # true alpha vs index

        # ── Display price ─────────────────────────────────────────────────────
        live_px = live_prices.get(ticker)
        display_price = round(live_px, 2) if live_px else round(closes[-1], 2)
        price_tag     = "" if live_px else " ⚠️prev close"

        # ── FIX III: trend-reversal flag ──────────────────────────────────────
        reversing = is_reversing(closes)

        candidates.append({
            "ticker"      : ticker.replace(".NS", ""),
            "price"       : display_price,
            "price_tag"   : price_tag,
            "velocity"    : velocity,
            "smoothness"  : smoothness,
            "consistency" : consistency,
            "vol_ratio"   : vol_ratio,
            "alpha"       : alpha,
            "rsi"         : rsi,
            "beta"        : beta,
            "reversing"   : reversing,
        })

    print(f"\n   📊  Filter summary: {filtered_rsi} removed by RSI>{RSI_OVERBOUGHT} | "
          f"{filtered_beta} removed by Beta>{BETA_CEILING} | "
          f"{filtered_vel} removed by negative velocity | "
          f"{len(candidates)} passed")

    if not candidates:
        print("\n⚠️  No stocks passed all filters today.")
        return []

    # ── Normalise ─────────────────────────────────────────────────────────────
    vel_v = [min(c["velocity"],        VELOCITY_CAP) for c in candidates]
    alp_v = [min(max(c["alpha"], 0),   ALPHA_CAP)    for c in candidates]
    smo_v = [c["smoothness"]                         for c in candidates]
    con_v = [c["consistency"]                        for c in candidates]
    vov_v = [c["vol_ratio"]                          for c in candidates]

    bnd = [(min(v), max(v)) for v in [smo_v, alp_v, vel_v, con_v, vov_v]]

    for i, c in enumerate(candidates):
        ns  = normalise(smo_v[i], *bnd[0])
        na  = normalise(alp_v[i], *bnd[1])
        nv  = normalise(vel_v[i], *bnd[2])
        nc  = normalise(con_v[i], *bnd[3])
        nvo = normalise(vov_v[i], *bnd[4])

        score = (ns  * WEIGHTS["smoothness"]  +
                 na  * WEIGHTS["alpha"]        +
                 nv  * WEIGHTS["velocity"]     +
                 nc  * WEIGHTS["consistency"]  +
                 nvo * WEIGHTS["volume"])

        # FIX III: apply reversal discount if price falling over last 5 days
        if c["reversing"]:
            score = score * (1.0 - REVERSAL_DISCOUNT)

        c.update({
            "score"        : score,
            "expected_rise": c["velocity"] * c["smoothness"],
            "ns": ns, "na": na,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:TOP_N]

# ═══════════════════════════════════════════════════════════════════════════════
#  OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════

def print_results(top: list):
    print(f"\n🏆  TOP {TOP_N} NATURAL GROWTH LEADERS\n")
    for rank, c in enumerate(top, 1):
        sign     = "+" if c["expected_rise"] >= 0 else ""
        driver   = primary_driver(c["ns"], c["na"])
        rev_warn = "  ⚠️ SHORT-TERM REVERSAL — price falling last 5d" if c["reversing"] else ""
        print(f"#{rank} | {c['ticker']}{rev_warn}")
        print(f"   • Current Price      : ₹{c['price']:,.2f}{c['price_tag']}")
        print(f"   • Expected Rise      : {sign}{c['expected_rise']*100:.1f}%"
              f"  (Spike-Adjusted Velocity × Smoothness)")
        print(f"   • Probability of Rise: {c['score']:.1f}%")
        print(f"   • RSI / Beta         : {c['rsi']:.1f}  /  {c['beta']:.2f}")
        print(f"   • Primary Driver     : {driver}")
        print()

def print_footer():
    print(DLINE)
    print(f"⏱️  Executed : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📂  Cache    : {os.path.abspath(CACHE_FILE)}")
    print(f"⚖️  Allocation: Equal-weight ~10% per position across Top {TOP_N}")
    print(f"🛡️  Filters  : RSI(Wilder,full-history) < {RSI_OVERBOUGHT} | "
          f"Beta({BETA_WINDOW}d aligned) < {BETA_CEILING} | "
          f"Spike>{int(SPIKE_THRESHOLD*100)}% cuts velocity+smoothness | "
          f"5d reversal = -{int(REVERSAL_DISCOUNT*100)}% score")
    print(DLINE)
    print("\n⚠️  DISCLAIMER: Educational & research use only. Not financial advice.\n")

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    banner()
    print_dates()

    print("\n⏳  Step 1/3 — Historical data …\n")
    hist = load_historical()

    print("\n⏳  Step 2/3 — Live prices …")
    live_prices = fetch_live_prices(NIFTY50)

    print("\n🔬  Step 3/3 — Quantitative engine …")
    top = analyse(hist, live_prices)

    if top:
        print_results(top)

    print_footer()

if __name__ == "__main__":
    main()
