"""
可配置回测引擎 — 用户自定义触发档位、买入标的、定投规则
"""
import math
import os
import json
import time as time_mod

import yfinance as yf
import pandas as pd
import numpy as np


def _fetch_one(ticker, start, end, max_retries=4):
    for attempt in range(max_retries):
        try:
            tk = yf.Ticker(ticker)
            df = tk.history(start=start, end=end, auto_adjust=True)
            if df.empty:
                raise ValueError(f"Empty data for {ticker}")
            return df["Close"]
        except Exception as e:
            wait = (attempt + 1) * 5
            print(f"  {ticker} failed ({attempt+1}/{max_retries}), retry in {wait}s...")
            time_mod.sleep(wait)
    raise RuntimeError(f"Failed to fetch {ticker}")


def fetch_data(tickers, start, end, cache_key=None):
    """Fetch close prices for a list of tickers. Uses local CSV cache."""
    # Check for named cache files first (real/sim)
    for prefix in ["real", "sim"]:
        cf = f"cache_{prefix}.csv"
        cp = os.path.join(os.path.dirname(os.path.abspath(__file__)), cf)
        if os.path.exists(cp):
            cached = pd.read_csv(cp, index_col=0)
            cached.index = pd.to_datetime(cached.index, utc=True).tz_localize(None)
            cols = set(cached.columns)
            if cols >= set(tickers):
                c_start = cached.index[0].strftime("%Y-%m-%d")
                c_end = cached.index[-1].strftime("%Y-%m-%d")
                if c_start <= start and c_end >= end:
                    print(f"  Using cache: {cp}")
                    return cached.loc[start:end][list(tickers)]

    if cache_key:
        cache_file = f"cache_{cache_key}.csv"
        cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), cache_file)
        if os.path.exists(cache_path):
            print(f"  Loading from cache: {cache_path}")
            df = pd.read_csv(cache_path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(None)
            return df

    closes = {}
    for t in tickers:
        print(f"  Fetching {t}...")
        closes[t] = _fetch_one(t, start, end)
    result = pd.DataFrame(closes).ffill()

    if cache_key:
        result.to_csv(cache_path)
        print(f"  Cached to {cache_path}")
    return result


def run_backtest(params):
    """
    Run a configurable drawdown-ladder backtest.

    params dict:
      - symbol: str — underlying ticker for drawdown calculation
      - start: str, end: str — date range
      - ath_reset: bool — reset triggers on new ATH
      - rapid_rally: {enabled, days, pct} or None
      - triggers: [{drawdown_pct: float, buys: [{ticker, mode: "shares"|"amount", value}]}]
        drawdown_pct is negative, e.g. -0.10 for -10%
        mode="shares": buy exactly `value` shares
        mode="amount": buy ceil(value / price) shares (integer shares)
      - dca: {enabled, ticker, amount_per_day} or None
    """
    symbol = params["symbol"]
    start = params["start"]
    end = params["end"]
    ath_reset = params.get("ath_reset", True)
    triggers = params.get("triggers", [])
    dca = params.get("dca") or {}
    rr = params.get("rapid_rally") or {}

    # Collect all tickers needed
    all_tickers = {symbol}
    for tr in triggers:
        for b in tr.get("buys", []):
            all_tickers.add(b["ticker"])
    if dca.get("enabled"):
        all_tickers.add(dca.get("ticker", symbol))

    # Fetch data for core tickers
    cache_key = dca.get("cache_key") or hash(json.dumps(sorted(all_tickers), sort_keys=True) + start + end) % 100000
    closes = fetch_data(list(all_tickers), start, end, cache_key=str(cache_key))

    # Try to fetch SPY separately (may fail if rate-limited, cache doesn't have it)
    has_spy = False
    if "SPY" not in closes.columns:
        try:
            spy_data = fetch_data(["SPY"], start, end, cache_key="spy_"+str(cache_key))
            if "SPY" in spy_data.columns:
                closes["SPY"] = spy_data["SPY"]
                has_spy = True
        except Exception:
            pass  # SPY unavailable, results won't show it
    else:
        has_spy = True

    underlying = closes[symbol]
    dates = closes.index
    dates_str = [d.strftime("%Y-%m-%d") for d in dates]

    # ── Sort triggers by drawdown (deepest first for matching) ──
    triggers_sorted = sorted(triggers, key=lambda t: t["drawdown_pct"])  # -0.70, -0.50, -0.30, -0.20, -0.10

    # ── Multiple trigger sets for rapid rally ──
    rapid_enabled = rr.get("enabled", False)
    rapid_days = int(rr.get("days", 21))
    rapid_pct = float(rr.get("pct", 0.10))
    rapid_triggers = rr.get("rapid_triggers", None)  # optional separate trigger list for rapid mode
    if rapid_enabled and rapid_triggers is None:
        # Default: skip shallowest trigger in rapid mode
        if len(triggers_sorted) > 1:
            rapid_triggers = triggers_sorted[1:]  # skip the shallowest
        else:
            rapid_triggers = triggers_sorted

    # ── Run simulation ──
    trades = []
    triggered = [False] * len(triggers_sorted)
    ath = underlying.iloc[0]
    ath_date = dates[0]
    ath_log = []
    current_mode = "normal"
    current_triggers = triggers_sorted

    for i in range(len(closes)):
        date = dates[i]
        price = underlying.iloc[i]

        if ath_reset and price > ath:
            # New ATH — check rapid rally
            if rapid_enabled:
                ath_idx = closes.index.get_indexer([date], method='nearest')[0]
                lookback_idx = max(0, ath_idx - rapid_days)
                past_price = underlying.iloc[lookback_idx]
                rally_pct = (price - past_price) / past_price
                if rally_pct > rapid_pct and rapid_triggers:
                    current_mode = "rapid"
                    current_triggers = rapid_triggers
                else:
                    current_mode = "normal"
                    current_triggers = triggers_sorted
            else:
                current_mode = "normal"
                current_triggers = triggers_sorted

            ath = price
            ath_date = date
            triggered = [False] * len(current_triggers)
            ath_log.append({
                "date": date.strftime("%Y-%m-%d"),
                "price": round(float(ath), 2),
                "mode": current_mode,
            })
            continue

        # Check triggers
        dd = (price - ath) / ath  # negative value
        trig_items = list(enumerate(current_triggers))

        for j, tdef in trig_items:
            if dd > tdef["drawdown_pct"]:  # not deep enough yet
                continue
            if triggered[j]:
                continue
            triggered[j] = True
            for b in tdef.get("buys", []):
                ticker = b["ticker"]
                buy_price = closes[ticker].iloc[i]
                if pd.isna(buy_price) or buy_price <= 0:
                    continue
                mode = b.get("mode", "amount")
                val = float(b["value"])
                if mode == "shares":
                    shares = val
                    cost = shares * buy_price
                else:
                    # amount mode — integer shares
                    shares = math.ceil(val / buy_price)
                    cost = shares * buy_price
                trades.append({
                    "ath_date": ath_date.strftime("%Y-%m-%d"),
                    "ath_price": round(float(ath), 2),
                    "mode": current_mode,
                    "date": date.strftime("%Y-%m-%d"),
                    "trigger": f'{tdef["drawdown_pct"]*100:.0f}%',
                    "dd_actual": f"{dd*100:.1f}%",
                    "ticker": ticker,
                    "price": round(float(buy_price), 2),
                    "shares": shares,
                    "cost": round(float(cost), 2),
                })

    # ── DCA ──
    dca_records = []
    dca_daily_val = [0.0] * len(closes)
    dca_daily_cost = [0.0] * len(closes)
    if dca.get("enabled"):
        dca_ticker = dca.get("ticker", symbol)
        dca_amount = float(dca.get("amount_per_day", 0))
        dca_end = dca.get("end_date", end)
        running_shares = 0.0
        running_cost = 0.0
        for i in range(len(closes)):
            date_str = dates_str[i]
            if date_str <= dca_end:
                px = closes[dca_ticker].iloc[i]
                if not pd.isna(px) and px > 0:
                    running_shares += dca_amount / px
                    running_cost += dca_amount
            dca_daily_val[i] = round(float(running_shares * closes[dca_ticker].iloc[i]), 2)
            dca_daily_cost[i] = round(float(running_cost), 2)
        dca_records = {
            "ticker": dca_ticker,
            "amount_per_day": dca_amount,
            "end_date": dca_end,
            "total_cost": round(float(running_cost), 2),
            "final_value": dca_daily_val[-1],
        }

    # ── Daily values for the main strategy ──
    # Aggregate by ticker
    ticker_shares = {}
    for t in trades:
        tkr = t["ticker"]
        if tkr not in ticker_shares:
            ticker_shares[tkr] = [0.0] * len(closes)
    for t in trades:
        tkr = t["ticker"]
        trade_idx = dates_str.index(t["date"]) if t["date"] in dates_str else 0
        for j in range(trade_idx, len(closes)):
            ticker_shares[tkr][j] += t["shares"]

    strategy_val = [0.0] * len(closes)
    for tkr, sh_list in ticker_shares.items():
        for i in range(len(closes)):
            strategy_val[i] += sh_list[i] * float(closes[tkr].iloc[i])
    strategy_val = [round(v, 2) for v in strategy_val]

    # Combined (strategy + DCA)
    combined_val = [round(strategy_val[i] + dca_daily_val[i], 2) for i in range(len(closes))]

    # ── Per-ticker breakdown ──
    ticker_breakdown = []
    for tkr in sorted(ticker_shares.keys()):
        sub = [t for t in trades if t["ticker"] == tkr]
        final_sh = ticker_shares[tkr][-1] if ticker_shares[tkr] else 0
        cost_t = sum(t["cost"] for t in sub)
        cur_px = float(closes[tkr].iloc[-1])
        val_t = final_sh * cur_px
        pnl = val_t - cost_t
        pnl_pct = (pnl / cost_t * 100) if cost_t > 0 else 0
        ticker_breakdown.append({
            "ticker": tkr, "shares": round(final_sh, 2),
            "cost": round(cost_t, 2), "price": round(cur_px, 2),
            "value": round(val_t, 2), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })

    # ── Summary ──
    total_cost = sum(t["cost"] for t in trades)
    final_val = strategy_val[-1]
    combined_cost = total_cost + (dca_records["total_cost"] if dca_records else 0)
    combined_final = combined_val[-1]

    # Max drawdowns
    def calc_max_dd(vals):
        peak = 0
        max_dd = 0
        for v in vals:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (v - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd
        return round(max_dd, 2)

    # B&H: if you invested the same total cost at day 0
    bh_val = total_cost * float(underlying.iloc[-1]) / float(underlying.iloc[0]) if total_cost > 0 else 0
    bh_pct = (float(underlying.iloc[-1]) / float(underlying.iloc[0]) - 1) * 100
    bh_max_dd = calc_max_dd([float(x) for x in underlying])

    combined_bh_val = combined_cost * float(underlying.iloc[-1]) / float(underlying.iloc[0]) if combined_cost > 0 else 0

    # ── Metrics: CAGR & Sharpe ──
    from datetime import datetime as _dt
    years = max((_dt.strptime(end, "%Y-%m-%d") - _dt.strptime(start, "%Y-%m-%d")).days / 365.25, 0.25)

    def calc_cagr(cost, final_val):
        if cost <= 0 or final_val <= 0:
            return 0
        return round(((final_val / cost) ** (1 / years) - 1) * 100, 2)

    def calc_sharpe(vals, cost):
        """Annualized Sharpe from daily values. Assumes 252 trading days/year, rf=0 for simplicity."""
        if cost <= 0 or len(vals) < 2:
            return None
        daily_r = []
        for i in range(1, len(vals)):
            if vals[i-1] > 0:
                daily_r.append((vals[i] - vals[i-1]) / vals[i-1])
        if len(daily_r) < 2:
            return None
        mean_r = sum(daily_r) / len(daily_r)
        var = sum((r - mean_r) ** 2 for r in daily_r) / (len(daily_r) - 1)
        std_r = var ** 0.5
        if std_r == 0:
            return None
        return round(mean_r / std_r * (252 ** 0.5), 2)

    def calc_sharpe_prices(prices):
        """Sharpe for buy-and-hold price series."""
        if len(prices) < 2:
            return None
        daily_r = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices)) if prices[i-1] > 0]
        if len(daily_r) < 2:
            return None
        mean_r = sum(daily_r) / len(daily_r)
        var = sum((r - mean_r) ** 2 for r in daily_r) / (len(daily_r) - 1)
        std_r = var ** 0.5
        if std_r == 0:
            return None
        return round(mean_r / std_r * (252 ** 0.5), 2)

    # ── SPY benchmark ──
    spy_prices = None
    spy_bh = None
    if has_spy and "SPY" in closes.columns:
        spy_series = closes["SPY"]
        spy_prices = [round(float(x), 2) for x in spy_series.tolist()]
        ref_cost = total_cost if total_cost > 0 else combined_cost
        spy_bh_val = ref_cost * float(spy_series.iloc[-1]) / float(spy_series.iloc[0]) if ref_cost > 0 else 0
        spy_sharpe = calc_sharpe_prices([float(x) for x in spy_series])
        spy_bh = {
            "cost": round(ref_cost, 2),
            "final_value": round(float(spy_bh_val), 2),
            "return_pct": round((float(spy_series.iloc[-1]) / float(spy_series.iloc[0]) - 1) * 100, 2),
            "max_dd_pct": calc_max_dd([float(x) for x in spy_series]),
            "cagr": round(((float(spy_series.iloc[-1]) / float(spy_series.iloc[0])) ** (1 / years) - 1) * 100, 2),
            "sharpe": spy_sharpe,
        }

    return {
        "config": {
            "symbol": symbol, "start": start, "end": end,
            "ath_reset": ath_reset,
            "rapid_rally": {"enabled": rapid_enabled, "days": rapid_days, "pct": rapid_pct},
            "triggers": [{"drawdown_pct": t["drawdown_pct"], "buys_count": len(t.get("buys", []))} for t in triggers_sorted],
            "dca": dca_records,
        },
        "dates": dates_str,
        "trades": trades,
        "ath_log": ath_log,
        "strategy_val": strategy_val,
        "dca_val": dca_daily_val,
        "combined_val": combined_val,
        "spy_prices": spy_prices,
        "spy_benchmark": spy_bh,
        "ticker_breakdown": ticker_breakdown,
        "summary": {
            "total_cost": round(total_cost, 2),
            "final_value": round(final_val, 2),
            "return_pct": round((final_val / total_cost - 1) * 100, 2) if total_cost > 0 else 0,
            "cagr": calc_cagr(total_cost, final_val),
            "sharpe": calc_sharpe(strategy_val, total_cost),
            "max_dd_pct": calc_max_dd(strategy_val),
            "dca_cost": dca_records["total_cost"] if dca_records else 0,
            "dca_value": dca_records["final_value"] if dca_records else 0,
            "dca_return_pct": round((dca_records["final_value"] / dca_records["total_cost"] - 1) * 100, 2) if dca_records and dca_records["total_cost"] > 0 else 0,
            "dca_cagr": calc_cagr(dca_records["total_cost"] if dca_records else 0, dca_records["final_value"] if dca_records else 0) if dca_records else 0,
            "combined_cost": round(combined_cost, 2),
            "combined_value": round(combined_final, 2),
            "combined_return_pct": round((combined_final / combined_cost - 1) * 100, 2) if combined_cost > 0 else 0,
            "combined_cagr": calc_cagr(combined_cost, combined_final),
            "combined_sharpe": calc_sharpe(combined_val, combined_cost),
            "combined_max_dd_pct": calc_max_dd(combined_val),
            "trade_count": len(trades),
            "years": round(years, 1),
        },
        "buy_hold": {
            "symbol": symbol,
            "cost": round(total_cost if total_cost > 0 else combined_cost, 2),
            "final_value": round(float(bh_val), 2) if total_cost > 0 else round(float(combined_bh_val), 2),
            "return_pct": round(bh_pct, 2),
            "cagr": round(((float(underlying.iloc[-1]) / float(underlying.iloc[0])) ** (1 / years) - 1) * 100, 2),
            "sharpe": calc_sharpe_prices([float(x) for x in underlying]),
            "max_dd_pct": round(bh_max_dd, 2),
        },
        "underlying_prices": [round(float(x), 2) for x in underlying.tolist()],
    }
