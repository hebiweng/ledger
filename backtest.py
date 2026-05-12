"""
可配置回测引擎 — 用户自定义触发档位、买入标的、定投规则
"""
import math
import os
import json
import time as time_mod
from datetime import datetime

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


def _cache_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def _load_from_market_prices(tickers, start, end):
    """Try loading price data from the unified market_prices table."""
    try:
        import sqlite3
        conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db"))
        placeholders = ','.join(['?'] * len(tickers))
        cur = conn.execute(
            f"SELECT ticker, date, close_price FROM market_prices WHERE ticker IN ({placeholders}) AND date >= ? AND date <= ? ORDER BY date",
            (*tickers, start, end)
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return None
        data = {}
        for ticker, date, price in rows:
            if ticker not in data:
                data[ticker] = {}
            data[ticker][date] = price
        df = pd.DataFrame(data)
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        cols = set(df.columns)
        if cols >= set(tickers):
            c_start = df.index[0].strftime("%Y-%m-%d")
            c_end = df.index[-1].strftime("%Y-%m-%d")
            if c_start <= start and c_end >= end:
                return df.loc[start:end][list(tickers)]
    except Exception as e:
        print(f"  DB read skipped: {e}")
    return None


def _save_to_market_prices(df):
    """Save fetched price data into market_prices table."""
    try:
        import sqlite3
        conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db"))
        cur = conn.cursor()
        count = 0
        for date, row in df.iterrows():
            date_str = date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)[:10]
            for ticker in df.columns:
                px = float(row[ticker])
                if pd.notna(px) and px > 0:
                    cur.execute(
                        'INSERT OR IGNORE INTO market_prices(ticker, date, close_price, source, updated_at) VALUES(?,?,?,?,datetime("now"))',
                        (ticker, date_str, px, 'yfinance')
                    )
                    count += 1
        conn.commit()
        conn.close()
        if count > 0:
            print(f"  Saved {count} rows to market_prices")
    except Exception as e:
        print(f"  DB save skipped: {e}")


def fetch_data(tickers, start, end, cache_key=None):
    """Fetch close prices for a list of tickers. Uses market_prices DB → CSV → yfinance."""
    cdir = _cache_dir()
    os.makedirs(cdir, exist_ok=True)

    # 1. Try unified market_prices DB table
    db_df = _load_from_market_prices(tickers, start, end)
    if db_df is not None:
        print(f"  Using DB: market_prices table")
        return db_df

    # 2. Fall back to named CSV cache (legacy)
    for prefix in ["real", "sim"]:
        cf = f"cache_{prefix}.csv"
        cp = os.path.join(cdir, cf)
        if os.path.exists(cp):
            cached = pd.read_csv(cp, index_col=0)
            cached.index = pd.to_datetime(cached.index, utc=True).tz_localize(None)
            cols = set(cached.columns)
            if cols >= set(tickers):
                c_start = cached.index[0].strftime("%Y-%m-%d")
                c_end = cached.index[-1].strftime("%Y-%m-%d")
                if c_start <= start and c_end >= end:
                    print(f"  Using legacy CSV: {cp}")
                    return cached.loc[start:end][list(tickers)]

    # 3. Session CSV cache
    if cache_key:
        cache_path = os.path.join(cdir, f"cache_{cache_key}.csv")
        if os.path.exists(cache_path):
            print(f"  Loading from CSV cache: {cache_path}")
            df = pd.read_csv(cache_path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(None)
            return df
    else:
        cache_path = None

    # 4. Fetch from yfinance
    closes = {}
    for t in tickers:
        print(f"  Fetching {t}...")
        closes[t] = _fetch_one(t, start, end)
    result = pd.DataFrame(closes).ffill()

    # Save to both session cache and DB
    if cache_path:
        result.to_csv(cache_path)
        print(f"  Cached to {cache_path}")
    _save_to_market_prices(result)

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
    seed_raw = params.get("seed")
    if isinstance(seed_raw, dict):
        seed = seed_raw
    elif seed_raw and float(seed_raw) > 0:
        seed = {"ticker": symbol, "amount": float(seed_raw)}
    else:
        seed = {}

    # Collect all tickers needed
    all_tickers = {symbol}
    for tr in triggers:
        for b in tr.get("buys", []):
            all_tickers.add(b["ticker"])
    if isinstance(seed, dict) and seed.get("amount", 0) > 0:
        all_tickers.add(seed.get("ticker", symbol))
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
    # Seed capital (底仓) — buy on day 0
    if isinstance(seed, dict) and seed.get("amount", 0) > 0:
        seed_ticker = seed.get("ticker", symbol)
        seed_px = float(closes[seed_ticker].iloc[0])
        seed_amt = float(seed["amount"])
        if not pd.isna(seed_px) and seed_px > 0:
            seed_shares = math.ceil(seed_amt / seed_px)
            trades.append({
                "ath_date": dates[0].strftime("%Y-%m-%d"),
                "ath_price": round(float(underlying.iloc[0]), 2),
                "mode": "normal",
                "date": dates[0].strftime("%Y-%m-%d"),
                "trigger": "底仓",
                "dd_actual": "0.0%",
                "ticker": seed_ticker,
                "price": round(float(seed_px), 2),
                "shares": seed_shares,
                "cost": round(float(seed_shares * seed_px), 2),
            })

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
        dca_amount = float(dca.get("amount", dca.get("amount_per_day", 0)))
        dca_freq = dca.get("frequency", "daily")
        dca_end = dca.get("end_date", end)
        running_shares = 0.0
        running_cost = 0.0
        _last_period = None
        for i in range(len(closes)):
            date_str = dates_str[i]
            if date_str > dca_end:
                dca_daily_val[i] = round(float(running_shares * closes[dca_ticker].iloc[i]), 2)
                dca_daily_cost[i] = round(float(running_cost), 2)
                continue
            px = closes[dca_ticker].iloc[i]
            if pd.isna(px) or px <= 0:
                dca_daily_val[i] = 0.0
                dca_daily_cost[i] = round(float(running_cost), 2)
                continue
            # Determine if this day triggers a buy
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if dca_freq == "daily":
                trigger = True
                period_key = date_str
            elif dca_freq == "weekly":
                iso = dt.isocalendar()
                period_key = f"{iso[0]}-W{iso[1]:02d}"
                trigger = (period_key != _last_period)
            elif dca_freq == "monthly":
                period_key = date_str[:7]
                trigger = (period_key != _last_period)
            elif dca_freq == "yearly":
                period_key = date_str[:4]
                trigger = (period_key != _last_period)
            else:
                trigger = True
                period_key = date_str
            if trigger:
                running_shares += dca_amount / float(px)
                running_cost += dca_amount
                _last_period = period_key
            dca_daily_val[i] = round(float(running_shares * closes[dca_ticker].iloc[i]), 2)
            dca_daily_cost[i] = round(float(running_cost), 2)
        freq_labels = {"daily": "日", "weekly": "周", "monthly": "月", "yearly": "年"}
        dca_records = {
            "enabled": True,
            "ticker": dca_ticker,
            "amount": dca_amount,
            "frequency": dca_freq,
            "frequency_label": freq_labels.get(dca_freq, dca_freq),
            "end_date": dca_end,
            "total_cost": round(float(running_cost), 2),
            "final_value": dca_daily_val[-1],
        }

        # ── Monthly tables: prep data for deferred emission ──
        _mo = {}
        _mo["month_last_idx"] = {}
        _mo["dca_shares_run"] = []
        _mo["dca_cost_run"] = []
        _mo["dca_ticker"] = dca_ticker
        run_sh = 0.0
        run_co = 0.0
        for i in range(len(closes)):
            ym = dates_str[i][:7]
            _mo["month_last_idx"][ym] = i
            date_str = dates_str[i]
            if date_str <= dca_end:
                px = float(closes[dca_ticker].iloc[i])
                if not pd.isna(px) and px > 0:
                    run_sh += dca_amount / px
                    run_co += dca_amount
            _mo["dca_shares_run"].append(run_sh)
            _mo["dca_cost_run"].append(run_co)
        _mo["trig_cost_by_month"] = {}
        for t in trades:
            ym = t["date"][:7]
            _mo["trig_cost_by_month"][ym] = _mo["trig_cost_by_month"].get(ym, 0.0) + t["cost"]

        dca_trades = True  # flag, actual rows built after combined_val
    else:
        # No DCA: build minimal _mo for monthly/annual tables
        _mo = {}
        _mo["month_last_idx"] = {}
        _mo["dca_shares_run"] = [0.0] * len(dates_str)
        _mo["dca_cost_run"] = [0.0] * len(dates_str)
        _mo["dca_ticker"] = symbol
        for i in range(len(dates_str)):
            _mo["month_last_idx"][dates_str[i][:7]] = i
        _mo["trig_cost_by_month"] = {}
        for t in trades:
            ym = t["date"][:7]
            _mo["trig_cost_by_month"][ym] = _mo["trig_cost_by_month"].get(ym, 0.0) + t["cost"]
        dca_trades = True

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

    # ── Monthly table emission (deferred: needs combined_val) ──
    if dca_trades == True:
        combined_monthly = []
        dca_only_monthly = []
        prev_dca_sh = 0.0
        prev_dca_co = 0.0
        cum_trig = 0.0
        dca_tk = _mo["dca_ticker"]
        for ym in sorted(_mo["month_last_idx"].keys()):
            idx = _mo["month_last_idx"][ym]
            trig_m = _mo["trig_cost_by_month"].get(ym, 0.0)
            cum_trig += trig_m
            dca_sh = _mo["dca_shares_run"][idx]
            dca_co = _mo["dca_cost_run"][idx]

            # Combined table (strategy + DCA, accurate via combined_val)
            cum_inv = cum_trig + dca_co
            cur_val = combined_val[idx]
            profit = round(cur_val - cum_inv, 2)
            ret = round(profit / cum_inv * 100, 2) if cum_inv > 0 else 0
            combined_monthly.append({
                "date": ym,
                "trig_cost": round(trig_m, 2),
                "dca_cost": round(dca_co - prev_dca_co, 2),
                "month_invested": round(trig_m + dca_co - prev_dca_co, 2),
                "cum_invested": round(cum_inv, 2),
                "cum_value": round(cur_val, 2),
                "cum_profit": profit,
                "ret_pct": ret,
            })

            # DCA-only table (single ticker, per-share accurate)
            dca_px = float(closes[dca_tk].iloc[idx])
            dca_val = dca_sh * dca_px
            dca_profit = round(dca_val - dca_co, 2)
            dca_ret = round(dca_profit / dca_co * 100, 2) if dca_co > 0 else 0
            dca_only_monthly.append({
                "date": ym,
                "month_cost": round(dca_co - prev_dca_co, 2),
                "month_shares": round(dca_sh - prev_dca_sh, 4),
                "cum_shares": round(dca_sh, 4),
                "cum_cost": round(dca_co, 2),
                "cum_value": round(dca_val, 2),
                "cum_profit": dca_profit,
                "ret_pct": dca_ret,
            })

            prev_dca_sh = dca_sh
            prev_dca_co = dca_co

        dca_trades = combined_monthly
        dca_only = dca_only_monthly

        # ── Annual aggregation ──
        year_last_idx = {}
        for i in range(len(dates_str)):
            year_last_idx[dates_str[i][:4]] = i
        annual = []
        prev_year_val = 0.0
        cum_trig_yr = 0.0
        prev_cum_inv = 0.0
        for yr in sorted(year_last_idx.keys()):
            idx = year_last_idx[yr]
            # Sum trigger cost for this year
            trig_yr = sum(t["cost"] for t in trades if t["date"][:4] == yr)
            # DCA cost for this year
            dca_co_yr = _mo["dca_cost_run"][idx]
            prev_yr_key = str(int(yr)-1)
            prev_dca = _mo["dca_cost_run"][year_last_idx[prev_yr_key]] if prev_yr_key in year_last_idx else 0
            dca_yr = dca_co_yr - prev_dca
            total_yr = round(trig_yr + dca_yr, 2)
            # Cumulative
            cum_inv_yr = dca_co_yr + sum(t["cost"] for t in trades if t["date"][:4] <= yr)
            cur_val_yr = combined_val[idx]
            profit_yr = round(cur_val_yr - cum_inv_yr, 2)
            ret_yr = round(profit_yr / cum_inv_yr * 100, 2) if cum_inv_yr > 0 else 0
            annual.append({
                "year": yr,
                "trig_cost": round(trig_yr, 2),
                "dca_cost": round(dca_yr, 2),
                "year_invested": total_yr,
                "cum_invested": round(cum_inv_yr, 2),
                "year_end_value": round(cur_val_yr, 2),
                "cum_profit": profit_yr,
                "ret_pct": ret_yr,
            })
    else:
        annual = []

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

    # Merge DCA into ticker breakdown
    if dca_records:
        dca_tk = dca_records["ticker"]
        dca_sh = _mo["dca_shares_run"][-1] if _mo else 0
        dca_co = dca_records["total_cost"]
        dca_px = float(closes[dca_tk].iloc[-1])
        dca_val = dca_sh * dca_px
        dca_pnl = dca_val - dca_co
        dca_pnl_pct = round(dca_pnl / dca_co * 100, 2) if dca_co > 0 else 0
        # Check if DCA ticker already exists in breakdown
        existing = next((tb for tb in ticker_breakdown if tb["ticker"] == dca_tk), None)
        if existing:
            existing["shares"] = round(existing["shares"] + dca_sh, 2)
            existing["cost"] = round(existing["cost"] + dca_co, 2)
            existing["value"] = round(existing["value"] + dca_val, 2)
            existing["pnl"] = round(existing["value"] - existing["cost"], 2)
            existing["pnl_pct"] = round(existing["pnl"] / existing["cost"] * 100, 2) if existing["cost"] > 0 else 0
        else:
            ticker_breakdown.insert(0, {
                "ticker": dca_tk, "shares": round(dca_sh, 2),
                "cost": round(dca_co, 2), "price": round(dca_px, 2),
                "value": round(dca_val, 2), "pnl": round(dca_pnl, 2),
                "pnl_pct": dca_pnl_pct,
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

    # B&H: same total capital as combined strategy
    seed = combined_cost if combined_cost > 0 else 1
    bh_val = seed * float(underlying.iloc[-1]) / float(underlying.iloc[0])
    bh_pct = (float(underlying.iloc[-1]) / float(underlying.iloc[0]) - 1) * 100
    bh_max_dd = calc_max_dd([float(x) for x in underlying])

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
        ref_cost = seed
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

    # NaN sanitizer for JSON compliance
    def _clean(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return 0.0
        if isinstance(v, dict):
            return {k: _clean(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_clean(vv) for vv in v]
        return v

    return _clean({
        "config": {
            "symbol": symbol, "start": start, "end": end,
            "ath_reset": ath_reset,
            "rapid_rally": {"enabled": rapid_enabled, "days": rapid_days, "pct": rapid_pct},
            "triggers": [{"drawdown_pct": t["drawdown_pct"], "buys": [{"ticker": b.get("ticker","?"), "mode": b.get("mode","amount"), "value": b.get("value",0)} for b in t.get("buys",[])]} for t in triggers_sorted],
            "dca": dca_records,
            "seed": {"ticker": seed.get("ticker", symbol), "amount": seed.get("amount", 0)} if isinstance(seed, dict) and seed.get("amount", 0) > 0 else None,
        },
        "dates": dates_str,
        "trades": trades,
        "dca_trades": dca_trades,
        "dca_only": dca_only,
        "annual": annual,
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
            "dca_max_dd_pct": calc_max_dd(dca_daily_val) if dca_records else None,
            "dca_sharpe": calc_sharpe(dca_daily_val, dca_records["total_cost"] if dca_records else 0) if dca_records else None,
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
            "cost": round(seed, 2),
            "final_value": round(float(bh_val), 2),
            "return_pct": round(bh_pct, 2),
            "cagr": round(((float(underlying.iloc[-1]) / float(underlying.iloc[0])) ** (1 / years) - 1) * 100, 2),
            "sharpe": calc_sharpe_prices([float(x) for x in underlying]),
            "max_dd_pct": round(bh_max_dd, 2),
        },
        "underlying_prices": [round(float(x), 2) for x in underlying.tolist()],
    })
