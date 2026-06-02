"""
================================================================================
  NIFTY 50 INSTITUTIONAL MOMENTUM ENGINE — 5-YEAR BACKTEST
================================================================================
  REQUIRES: pip install yfinance numpy pandas
  RUN IN  : Python IDLE, VS Code, Terminal — any standard Python 3.8+

  HOW IT WORKS
  ─────────────────────────────────────────────────────────────────────────────
  • Downloads 5+ years of daily OHLCV for all 50 Nifty stocks + ^NSEI index
  • Walks forward month by month (Jan 2020 → Apr 2026)
  • On each month's first trading day it runs the EXACT same 5-element engine
    (same weights, same filters, same spike penalty, same reversal guard)
    using only data available UP TO that date (zero look-ahead bias)
  • Selects Top 10 stocks, invests equal 10% capital in each
  • Holds for exactly 30 calendar days, then sells at that day's close
  • Tracks portfolio value, per-trade P&L, win rate, drawdown, Sharpe ratio
  • Compares against Buy-and-Hold Nifty 50 index over same period

  OUTPUT SECTIONS
  ─────────────────────────────────────────────────────────────────────────────
  1. Monthly trade log  — which stocks were picked, buy/sell price, return
  2. Month-by-month portfolio curve
  3. Final performance summary — CAGR, Sharpe, Max Drawdown, Win Rate, etc.
  4. Best and worst months
  5. Head-to-head vs Nifty 50 index
================================================================================
"""

import datetime
import warnings
import sys

import numpy as np

try:
    import pandas as pd
except ImportError:
    print("\n[SETUP] Run:  pip install pandas\n"); sys.exit(1)

try:
    import yfinance as yf
except ImportError:
    print("\n[SETUP] Run:  pip install yfinance\n"); sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
#  BACKTEST CONFIGURATION  — edit these if you want
# ═══════════════════════════════════════════════════════════════════════════════

BACKTEST_START   = "2020-01-01"   # 5+ years of history
BACKTEST_END     = "2026-05-01"   # up to last completed month
INITIAL_CAPITAL  = 100_000.0      # ₹ starting portfolio value
TOP_N            = 10             # stocks selected each month
HOLD_DAYS        = 30             # calendar days per trade

# Engine parameters (identical to live engine)
SCORE_DAYS       = 21
CONSISTENCY_DAYS = 20
VOLUME_SHORT     = 15
VOLUME_LONG      = 60
RSI_PERIOD       = 14
RSI_OVERBOUGHT   = 65
BETA_CEILING     = 1.3
BETA_WINDOW      = 90
SPIKE_THRESHOLD  = 0.06
RECENT_DAYS      = 5
VELOCITY_CAP     = 0.15
ALPHA_CAP        = 0.10
REVERSAL_DISCOUNT= 0.30
MIN_HISTORY_BARS = 80             # minimum bars needed before engine can run

WEIGHTS = {
    "smoothness"  : 0.30,
    "alpha"       : 0.25,
    "velocity"    : 0.20,
    "consistency" : 0.15,
    "volume"      : 0.10,
}

NIFTY50 = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","BHARTIARTL.NS","ICICIBANK.NS",
    "INFY.NS","SBIN.NS","HINDUNILVR.NS","ITC.NS","LT.NS",
    "KOTAKBANK.NS","AXISBANK.NS","BAJFINANCE.NS","ASIANPAINT.NS","MARUTI.NS",
    "TITAN.NS","NESTLEIND.NS","ULTRACEMCO.NS","WIPRO.NS","HCLTECH.NS",
    "POWERGRID.NS","NTPC.NS","SUNPHARMA.NS","ONGC.NS","JSWSTEEL.NS",
    "TATASTEEL.NS","ADANIENT.NS","ADANIPORTS.NS","COALINDIA.NS","INDUSINDBK.NS",
    "BAJAJFINSV.NS","BAJAJ-AUTO.NS","GRASIM.NS","HINDALCO.NS","CIPLA.NS",
    "EICHERMOT.NS","HEROMOTOCO.NS","DRREDDY.NS","DIVISLAB.NS","APOLLOHOSP.NS",
    "TECHM.NS","BPCL.NS","BRITANNIA.NS","SHRIRAMFIN.NS","TATACONSUM.NS",
    "TATAMOTORS.NS","M&M.NS","VEDL.NS","SBILIFE.NS","HDFCLIFE.NS",
]
BENCHMARK = "^NSEI"

LINE  = "=" * 80
DLINE = "-" * 80
SLINE = "─" * 80

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — DOWNLOAD ALL DATA (one single batch download for speed)
# ═══════════════════════════════════════════════════════════════════════════════

def download_all_data() -> dict:
    """
    Downloads full OHLCV history for all 50 stocks + benchmark in one call.
    Returns a dict keyed by ticker with a date-indexed DataFrame of
    {Close, Volume} columns.
    """
    all_tickers = NIFTY50 + [BENCHMARK]

    # fetch extra history before BACKTEST_START so the engine has
    # enough warmup bars on the very first month
    fetch_start = (
        pd.Timestamp(BACKTEST_START) - pd.DateOffset(months=6)
    ).strftime("%Y-%m-%d")

    print(f"   Downloading {len(all_tickers)} tickers from {fetch_start} …")
    print("   (This may take 60–120 seconds on first run)\n")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = yf.download(
            tickers     = all_tickers,
            start       = fetch_start,
            end         = BACKTEST_END,
            auto_adjust = True,
            progress    = True,
            threads     = True,
            timeout     = 60,
        )

    data = {}
    for ticker in all_tickers:
        try:
            close_s  = raw["Close"][ticker].dropna()
            volume_s = raw["Volume"][ticker].dropna()
            shared   = close_s.index.intersection(volume_s.index)
            if len(shared) > 50:
                df = pd.DataFrame({
                    "Close" : close_s.loc[shared].astype(float),
                    "Volume": volume_s.loc[shared].astype(float),
                })
                data[ticker] = df
        except Exception:
            pass

    loaded = sum(1 for t in NIFTY50 if t in data)
    print(f"\n   ✅  Loaded {loaded}/50 Nifty stocks + benchmark")
    if loaded < 20:
        raise RuntimeError("Too few tickers loaded. Check internet connection.")
    return data

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — ENGINE MATH  (identical to live engine, operates on sliced data)
# ═══════════════════════════════════════════════════════════════════════════════

def rsi_wilder(closes: np.ndarray, period: int = RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[:period].mean()
    avg_l  = losses[:period].mean()
    w = (period - 1.0) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = avg_g * w + g / period
        avg_l = avg_l * w + l / period
    return 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)

def beta_aligned(stock_df: pd.DataFrame, bench_df: pd.DataFrame,
                 window: int = BETA_WINDOW) -> float:
    try:
        combined = stock_df[["Close"]].join(
            bench_df[["Close"]], how="inner", lsuffix="_s", rsuffix="_b"
        ).tail(window)
        if len(combined) < 15:
            return 1.0
        sr = combined["Close_s"].pct_change().dropna().values
        br = combined["Close_b"].pct_change().dropna().values
        n  = min(len(sr), len(br))
        sr, br = sr[-n:], br[-n:]
        if n < 10:
            return 1.0
        cov = np.cov(sr, br, ddof=1)
        return 1.0 if cov[1,1] == 0 else float(cov[0,1] / cov[1,1])
    except Exception:
        return 1.0

def spike_penalty(closes: np.ndarray, days: int = SCORE_DAYS) -> float:
    if len(closes) < days:
        return 0.0
    p      = closes[-days:]
    rets   = np.abs(np.diff(p) / p[:-1])
    ms     = float(rets.max()) if len(rets) > 0 else 0.0
    if ms <= SPIKE_THRESHOLD:
        return 0.0
    return min((ms - SPIKE_THRESHOLD) / (0.20 - SPIKE_THRESHOLD), 1.0)

def velocity_adj(closes: np.ndarray, days: int = SCORE_DAYS,
                 pen: float = 0.0) -> float:
    if len(closes) < days + 1:
        return 0.0
    return ((closes[-1] / closes[-(days+1)]) - 1.0) * (1.0 - pen)

def raw_velocity(closes: np.ndarray, days: int = SCORE_DAYS) -> float:
    if len(closes) < days + 1:
        return 0.0
    return (closes[-1] / closes[-(days+1)]) - 1.0

def smoothness(closes: np.ndarray, days: int = SCORE_DAYS,
               pen: float = 0.0) -> float:
    if len(closes) < days:
        return 0.0
    p    = closes[-days:]
    x    = np.arange(days, dtype=float)
    corr = np.corrcoef(x, p)[0, 1]
    return max(0.0, corr**2 * (1.0 - pen))

def consistency(closes: np.ndarray, days: int = CONSISTENCY_DAYS) -> float:
    if len(closes) < days + 1:
        return 0.0
    sub   = closes[-(days+1):]
    green = sum(1 for i in range(1, len(sub)) if sub[i] > sub[i-1])
    return green / days

def volume_ratio(volumes: np.ndarray) -> float:
    if len(volumes) < VOLUME_LONG:
        return 1.0
    sa = np.mean(volumes[-VOLUME_SHORT:])
    la = np.mean(volumes[-VOLUME_LONG:])
    return 1.0 if la == 0 else float(sa / la)

def normalise(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 3 — SELECT TOP-N ON A GIVEN DATE  (no look-ahead bias)
# ═══════════════════════════════════════════════════════════════════════════════

def select_stocks(data: dict, as_of_date: pd.Timestamp) -> list:
    """
    Runs the full engine using only data up to (and including) as_of_date.
    Returns list of selected ticker strings.
    """
    bench_df = data.get(BENCHMARK)
    bench_slice = bench_df.loc[:as_of_date] if bench_df is not None else None
    bench_closes = bench_slice["Close"].values if bench_slice is not None and len(bench_slice) >= SCORE_DAYS + 5 else np.array([])
    bench_vel = raw_velocity(bench_closes) if len(bench_closes) >= SCORE_DAYS + 5 else 0.0

    candidates = []

    for ticker in NIFTY50:
        if ticker not in data:
            continue
        df = data[ticker].loc[:as_of_date]

        if len(df) < MIN_HISTORY_BARS:
            continue

        c_arr = df["Close"].values
        v_arr = df["Volume"].values

        # ── Risk shields ──────────────────────────────────────────────────────
        rsi = rsi_wilder(c_arr)
        if rsi > RSI_OVERBOUGHT:
            continue

        if bench_slice is not None and len(bench_slice) >= 15:
            b = beta_aligned(df, bench_slice)
        else:
            b = 1.0
        if b > BETA_CEILING:
            continue

        # ── Velocity ──────────────────────────────────────────────────────────
        rv = raw_velocity(c_arr)
        if rv <= 0:
            continue

        # ── 5 elements ────────────────────────────────────────────────────────
        sp   = spike_penalty(c_arr)
        vel  = velocity_adj(c_arr, pen=sp)
        smo  = smoothness(c_arr, pen=sp)
        con  = consistency(c_arr)
        volr = volume_ratio(v_arr)
        alp  = rv - bench_vel

        # ── Reversal guard ────────────────────────────────────────────────────
        reversing = (len(c_arr) >= RECENT_DAYS + 1 and
                     c_arr[-1] < c_arr[-(RECENT_DAYS+1)])

        candidates.append({
            "ticker"    : ticker,
            "velocity"  : vel,
            "smoothness": smo,
            "consistency": con,
            "vol_ratio" : volr,
            "alpha"     : alp,
            "reversing" : reversing,
        })

    if not candidates:
        return []

    # ── Normalise ─────────────────────────────────────────────────────────────
    vel_v = [min(c["velocity"],       VELOCITY_CAP) for c in candidates]
    alp_v = [min(max(c["alpha"],0),   ALPHA_CAP)    for c in candidates]
    smo_v = [c["smoothness"]                        for c in candidates]
    con_v = [c["consistency"]                       for c in candidates]
    vov_v = [c["vol_ratio"]                         for c in candidates]

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

        if c["reversing"]:
            score *= (1.0 - REVERSAL_DISCOUNT)

        c["score"] = score

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return [c["ticker"] for c in candidates[:TOP_N]]

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4 — MONTH ITERATOR  (generate all buy dates)
# ═══════════════════════════════════════════════════════════════════════════════

def get_trading_months(data: dict) -> list:
    """
    Returns list of (buy_date, sell_date) pairs — first trading day of each
    calendar month from BACKTEST_START to BACKTEST_END.
    """
    bench_df = data[BENCHMARK]
    all_dates = bench_df.loc[BACKTEST_START:BACKTEST_END].index

    months = []
    seen   = set()
    for d in all_dates:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            # find sell date: first trading day on or after buy_date + 30 cal days
            sell_target = d + pd.Timedelta(days=HOLD_DAYS)
            future = all_dates[all_dates >= sell_target]
            if len(future) == 0:
                break
            sell_date = future[0]
            months.append((d, sell_date))

    return months

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 5 — SIMULATE ONE MONTH
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_month(data: dict, buy_date: pd.Timestamp,
                   sell_date: pd.Timestamp, capital: float) -> dict:
    """
    Selects stocks, buys at buy_date close, sells at sell_date close.
    Returns result dict with per-stock and aggregate figures.
    """
    selected = select_stocks(data, buy_date)

    if not selected:
        return {
            "buy_date"   : buy_date,
            "sell_date"  : sell_date,
            "trades"     : [],
            "port_return": 0.0,
            "end_capital": capital,
            "skipped"    : True,
        }

    alloc  = capital / len(selected)
    trades = []

    for ticker in selected:
        df = data[ticker]

        # get buy price (close on buy_date or nearest available)
        try:
            buy_px = float(df.loc[:buy_date, "Close"].iloc[-1])
        except Exception:
            continue

        # get sell price (close on sell_date or nearest available)
        try:
            sell_px = float(df.loc[:sell_date, "Close"].iloc[-1])
        except Exception:
            sell_px = buy_px

        ret    = (sell_px / buy_px) - 1.0
        pnl    = alloc * ret
        trades.append({
            "ticker"  : ticker.replace(".NS", ""),
            "buy_px"  : round(buy_px, 2),
            "sell_px" : round(sell_px, 2),
            "return"  : ret,
            "pnl"     : round(pnl, 2),
        })

    if not trades:
        return {
            "buy_date"   : buy_date,
            "sell_date"  : sell_date,
            "trades"     : [],
            "port_return": 0.0,
            "end_capital": capital,
            "skipped"    : True,
        }

    port_ret   = np.mean([t["return"] for t in trades])
    end_capital = capital * (1.0 + port_ret)

    return {
        "buy_date"   : buy_date,
        "sell_date"  : sell_date,
        "trades"     : trades,
        "port_return": port_ret,
        "end_capital": round(end_capital, 2),
        "skipped"    : False,
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 6 — BENCHMARK RETURNS
# ═══════════════════════════════════════════════════════════════════════════════

def benchmark_return(data: dict, buy_date: pd.Timestamp,
                     sell_date: pd.Timestamp) -> float:
    df = data[BENCHMARK]
    try:
        buy_px  = float(df.loc[:buy_date,  "Close"].iloc[-1])
        sell_px = float(df.loc[:sell_date, "Close"].iloc[-1])
        return (sell_px / buy_px) - 1.0
    except Exception:
        return 0.0

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 7 — PERFORMANCE METRICS
# ═══════════════════════════════════════════════════════════════════════════════

def cagr(start_val: float, end_val: float, years: float) -> float:
    if years <= 0 or start_val <= 0:
        return 0.0
    return (end_val / start_val) ** (1.0 / years) - 1.0

def max_drawdown(equity_curve: list) -> float:
    peak = equity_curve[0]
    mdd  = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > mdd:
            mdd = dd
    return mdd

def sharpe_ratio(monthly_returns: list, risk_free_annual: float = 0.065) -> float:
    """Annualised Sharpe using monthly returns. Risk-free = 6.5% (India T-bill)."""
    if len(monthly_returns) < 2:
        return 0.0
    rf_monthly = (1 + risk_free_annual) ** (1/12) - 1
    excess = [r - rf_monthly for r in monthly_returns]
    if np.std(excess) == 0:
        return 0.0
    return float(np.mean(excess) / np.std(excess, ddof=1) * np.sqrt(12))

def sortino_ratio(monthly_returns: list, risk_free_annual: float = 0.065) -> float:
    rf_monthly = (1 + risk_free_annual) ** (1/12) - 1
    excess     = [r - rf_monthly for r in monthly_returns]
    downside   = [e for e in excess if e < 0]
    if len(downside) < 2 or np.std(downside) == 0:
        return 0.0
    return float(np.mean(excess) / np.std(downside, ddof=1) * np.sqrt(12))

# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 8 — PRINT ALL RESULTS
# ═══════════════════════════════════════════════════════════════════════════════

def print_results(results: list, equity: list, bench_equity: list,
                  months: list):

    print(f"\n{LINE}")
    print("  NIFTY 50 MOMENTUM ENGINE — 5-YEAR BACKTEST RESULTS")
    print(LINE)

    # ── Monthly trade log ─────────────────────────────────────────────────────
    print("\n📋  MONTH-BY-MONTH TRADE LOG\n")
    print(f"  {'Month':<12} {'Stocks Selected':<52} {'Return':>8}  {'Capital':>12}")
    print(f"  {SLINE}")

    for r in results:
        if r["skipped"]:
            label = "(no stocks passed filters)"
            tickers_str = label
        else:
            tickers_str = "  ".join(t["ticker"] for t in r["trades"])

        ret_str = f"{r['port_return']*100:+.2f}%"
        cap_str = f"₹{r['end_capital']:>11,.0f}"
        month_str = r["buy_date"].strftime("%b %Y")
        # truncate if too long
        if len(tickers_str) > 51:
            tickers_str = tickers_str[:48] + "..."
        print(f"  {month_str:<12} {tickers_str:<52} {ret_str:>8}  {cap_str:>12}")

    # ── Per-trade detail ──────────────────────────────────────────────────────
    print(f"\n\n{LINE}")
    print("  DETAILED TRADE BREAKDOWN — ALL MONTHS")
    print(LINE)

    for r in results:
        if r["skipped"]:
            continue
        bstr = r["buy_date"].strftime("%d %b %Y")
        sstr = r["sell_date"].strftime("%d %b %Y")
        ret_str = f"{r['port_return']*100:+.2f}%"
        print(f"\n  📅  {bstr}  →  {sstr}   |  Portfolio return: {ret_str}")
        print(f"  {'Ticker':<14} {'Buy ₹':>10}  {'Sell ₹':>10}  {'Return':>8}  {'P&L ₹':>10}")
        print(f"  {'─'*60}")
        for t in r["trades"]:
            ret_str_t = f"{t['return']*100:+.2f}%"
            win = "✅" if t["return"] >= 0 else "❌"
            print(f"  {win} {t['ticker']:<12} {t['buy_px']:>10,.2f}  "
                  f"{t['sell_px']:>10,.2f}  {ret_str_t:>8}  {t['pnl']:>10,.0f}")

    # ── Equity curve ──────────────────────────────────────────────────────────
    print(f"\n\n{LINE}")
    print("  PORTFOLIO EQUITY CURVE  (month-end values)")
    print(LINE)
    print(f"\n  {'Month':<12}  {'Strategy ₹':>14}  {'Nifty50 ₹':>14}  "
          f"{'Strategy':>10}  {'Nifty50':>10}  {'Alpha':>8}")
    print(f"  {SLINE}")

    for i, (r, eq, beq) in enumerate(zip(results, equity[1:], bench_equity[1:])):
        month_str = r["buy_date"].strftime("%b %Y")
        strat_ret = (eq / INITIAL_CAPITAL - 1) * 100
        bench_ret = (beq / INITIAL_CAPITAL - 1) * 100
        alpha_ret = strat_ret - bench_ret
        a_sign    = "▲" if alpha_ret >= 0 else "▼"
        print(f"  {month_str:<12}  {eq:>14,.0f}  {beq:>14,.0f}  "
              f"{strat_ret:>+9.1f}%  {bench_ret:>+9.1f}%  "
              f"{a_sign}{abs(alpha_ret):>6.1f}%")

    # ── Summary statistics ────────────────────────────────────────────────────
    monthly_rets   = [r["port_return"] for r in results if not r["skipped"]]
    bench_rets     = []
    bench_cap      = INITIAL_CAPITAL
    for bm, (buy_d, sell_d) in zip(results, months):
        br = benchmark_return_val(bm.get("_bench_ret", 0.0))
        bench_rets.append(br)

    bench_rets_clean = [r.get("_bench_ret", 0.0) for r in results]

    total_months  = len(results)
    active_months = len([r for r in results if not r["skipped"]])
    win_months    = len([r for r in results if not r["skipped"] and r["port_return"] > 0])
    loss_months   = active_months - win_months

    total_trades  = sum(len(r["trades"]) for r in results)
    win_trades    = sum(1 for r in results for t in r["trades"] if t["return"] >= 0)
    loss_trades   = total_trades - win_trades

    final_cap     = equity[-1]
    years         = total_months / 12.0

    strategy_cagr = cagr(INITIAL_CAPITAL, final_cap, years)
    bench_cagr    = cagr(INITIAL_CAPITAL, bench_equity[-1], years)

    strategy_mdd  = max_drawdown(equity)
    bench_mdd     = max_drawdown(bench_equity)

    strategy_sharpe  = sharpe_ratio(monthly_rets)
    strategy_sortino = sortino_ratio(monthly_rets)

    best_month  = max(results, key=lambda x: x["port_return"])
    worst_month = min(results, key=lambda x: x["port_return"])

    avg_monthly_ret  = np.mean(monthly_rets) if monthly_rets else 0
    avg_win_ret      = np.mean([r["port_return"] for r in results
                                if not r["skipped"] and r["port_return"] > 0] or [0])
    avg_loss_ret     = np.mean([r["port_return"] for r in results
                                if not r["skipped"] and r["port_return"] < 0] or [0])

    print(f"\n\n{LINE}")
    print("  📊  FINAL PERFORMANCE SUMMARY")
    print(LINE)

    print(f"""
  PERIOD ANALYSED
  ───────────────────────────────────────────────────
  Start Date          : {results[0]['buy_date'].strftime('%d %b %Y')}
  End Date            : {results[-1]['sell_date'].strftime('%d %b %Y')}
  Total Months Traded : {active_months}  (of {total_months} calendar months)
  Initial Capital     : ₹{INITIAL_CAPITAL:,.0f}
  Final Capital       : ₹{final_cap:,.0f}
  Net Profit          : ₹{final_cap - INITIAL_CAPITAL:,.0f}  ({(final_cap/INITIAL_CAPITAL - 1)*100:+.1f}%)

  RETURN METRICS
  ───────────────────────────────────────────────────
  Strategy CAGR       : {strategy_cagr*100:+.2f}%  per year
  Nifty 50  CAGR      : {bench_cagr*100:+.2f}%  per year
  Outperformance      : {(strategy_cagr - bench_cagr)*100:+.2f}%  per year (Alpha)

  Avg Monthly Return  : {avg_monthly_ret*100:+.2f}%
  Best  Month         : {best_month['buy_date'].strftime('%b %Y')}  {best_month['port_return']*100:+.2f}%
  Worst Month         : {worst_month['buy_date'].strftime('%b %Y')}  {worst_month['port_return']*100:+.2f}%

  RISK METRICS
  ───────────────────────────────────────────────────
  Max Drawdown (Strat): {strategy_mdd*100:.2f}%
  Max Drawdown (Nifty): {bench_mdd*100:.2f}%
  Sharpe Ratio        : {strategy_sharpe:.2f}  (annualised, rf=6.5%)
  Sortino Ratio       : {strategy_sortino:.2f}  (annualised)

  WIN / LOSS ANALYSIS
  ───────────────────────────────────────────────────
  Winning Months      : {win_months}  ({win_months/active_months*100 if active_months else 0:.1f}%)
  Losing  Months      : {loss_months}  ({loss_months/active_months*100 if active_months else 0:.1f}%)
  Avg Win  Month      : {avg_win_ret*100:+.2f}%
  Avg Loss Month      : {avg_loss_ret*100:+.2f}%
  Win/Loss Ratio      : {abs(avg_win_ret/avg_loss_ret) if avg_loss_ret != 0 else float('inf'):.2f}x

  Total Individual Trades : {total_trades}
  Winning Trades          : {win_trades}  ({win_trades/total_trades*100 if total_trades else 0:.1f}%)
  Losing  Trades          : {loss_trades}  ({loss_trades/total_trades*100 if total_trades else 0:.1f}%)
""")

    # ── Head to head ──────────────────────────────────────────────────────────
    print(f"  HEAD-TO-HEAD vs NIFTY 50")
    print(f"  {'─'*50}")
    strat_total = (final_cap / INITIAL_CAPITAL - 1) * 100
    bench_total = (bench_equity[-1] / INITIAL_CAPITAL - 1) * 100
    winner = "STRATEGY 🏆" if strat_total > bench_total else "NIFTY 50"
    print(f"  Strategy total return  : {strat_total:+.1f}%")
    print(f"  Nifty 50 total return  : {bench_total:+.1f}%")
    print(f"  Overall winner         : {winner}")
    print()
    print(f"  {'─'*50}")
    print("  ⚠️  DISCLAIMER: Past performance does not guarantee future results.")
    print("      This backtest is for educational purposes only.")
    print(f"  {'─'*50}\n")
    print(LINE)

def benchmark_return_val(v):
    return v

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{LINE}")
    print("  NIFTY 50 INSTITUTIONAL MOMENTUM ENGINE — 5-YEAR BACKTEST")
    print(f"  Period : {BACKTEST_START}  →  {BACKTEST_END}")
    print(f"  Capital: ₹{INITIAL_CAPITAL:,.0f}   |   Top {TOP_N} stocks   |   {HOLD_DAYS}-day holds")
    print(LINE)

    # ── Download ──────────────────────────────────────────────────────────────
    print("\n⏳  Step 1/4 — Downloading historical data …\n")
    data = download_all_data()

    # ── Get month list ────────────────────────────────────────────────────────
    print("\n⏳  Step 2/4 — Building month calendar …")
    months = get_trading_months(data)
    print(f"   Found {len(months)} monthly periods to backtest")

    # ── Run backtest ──────────────────────────────────────────────────────────
    print(f"\n⏳  Step 3/4 — Running backtest ({len(months)} months) …\n")

    results      = []
    equity       = [INITIAL_CAPITAL]
    bench_equity = [INITIAL_CAPITAL]
    capital      = INITIAL_CAPITAL
    bench_cap    = INITIAL_CAPITAL

    for idx, (buy_date, sell_date) in enumerate(months):
        pct = (idx + 1) / len(months) * 100
        sys.stdout.write(f"\r   Processing {buy_date.strftime('%b %Y')} … "
                         f"[{'█' * int(pct//5):<20}] {pct:.0f}%")
        sys.stdout.flush()

        result       = simulate_month(data, buy_date, sell_date, capital)
        bench_ret    = benchmark_return(data, buy_date, sell_date)
        result["_bench_ret"] = bench_ret

        capital      = result["end_capital"]
        bench_cap    = round(bench_cap * (1.0 + bench_ret), 2)

        results.append(result)
        equity.append(capital)
        bench_equity.append(bench_cap)

    print(f"\n   ✅  Backtest complete — {len(results)} months simulated")

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"\n⏳  Step 4/4 — Computing statistics and printing results …\n")
    print_results(results, equity, bench_equity, months)

if __name__ == "__main__":
    main()






