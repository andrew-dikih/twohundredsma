"""Generic stock-picking backtest harness.

A strategy is defined by 3 callables:
  - filter_fn(date, sigs, ctx) -> array[bool] of eligible tickers
  - score_fn(date, sigs, ctx) -> Series of scores per ticker (NaN = ineligible)
  - select_fn(date, scores, n) -> dict[ticker: weight] summing to 1.0

Plus parameters:
  - stock_w_base: base allocation to stocks vs BND
  - n_holdings: how many stocks
  - cadence: 'M' (monthly), 'Q' (quarterly), 'A' (annual)
  - dip_trigger: 'SPY_-10', None, etc -- moves bonds to stocks on market dip
  - hold_bias: minimum overlap between consecutive portfolios (e.g. 0.7 = need 30% change to retrade a name)
  - sector_cap: max weight per sector
  - min_sectors: minimum distinct sectors required

We log every run to dat/strategy_results.jsonl.
"""
import json
import pickle
import time
from typing import Dict
import numpy as np
import pandas as pd
from pathlib import Path
from _signal_engine import (
    load_prices_divs, compute_monthly_signals, get_etf_series,
    metrics, money_weighted_irr, benchmark_sso_bnd
)

RESULTS_FILE = 'strat_results/results.jsonl'
Path('strat_results').mkdir(exist_ok=True)


def load_fundamentals():
    return pickle.load(open('dat/fundamentals_2026-06.pkl', 'rb'))


def get_sector_map():
    f = load_fundamentals()
    if 'sector' not in f.columns:
        return {}
    raw = f['sector'].to_dict()
    # Normalize: NaN / None / empty / '?' -> 'Unknown' so all unknown-sector
    # tickers collapse into a SINGLE bucket and remain subject to the sector cap.
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if v is None or (isinstance(v, float) and v != v) or str(v).strip() in ('', 'nan', '?'):
            out[k] = 'Unknown'
        else:
            out[k] = str(v)
    return out


def get_enriched_sector_map():
    """get_sector_map() augmented with:
      - hardcoded ETF/fund sector classifications (BND, SSO, SPY, ...)
      - ticker_yield_cache.json sector entries (lazily populated by the
        advisor UI + a one-shot bulk yfinance enrichment script).

    Use this instead of get_sector_map() whenever you want the sector cap
    to fire on real sectors instead of an 'Unknown' bucket that masks
    REITs / Bonds / mortgage trusts (which yfinance classifies but the
    fundamentals pickle does not).
    """
    out = dict(get_sector_map())
    etf_sectors = {
        "BND": "Bonds", "AGG": "Bonds",
        "SSO": "Leveraged Equity ETF",
        "SPY": "Equity ETF", "VOO": "Equity ETF", "IVV": "Equity ETF",
        "VTI": "Equity ETF", "ITOT": "Equity ETF",
        "QQQ": "Equity ETF", "QQQM": "Equity ETF",
        "VEA": "Equity ETF", "VWO": "Equity ETF", "VXUS": "Equity ETF",
        "SCHD": "Equity ETF", "VYM": "Equity ETF", "HDV": "Equity ETF",
    }
    for sym, sec in etf_sectors.items():
        out[sym] = sec
    try:
        import json as _json, pathlib as _pl
        cache_path = _pl.Path(__file__).resolve().parent / "dat" / "ticker_yield_cache.json"
        if cache_path.exists():
            cache = _json.loads(cache_path.read_text(encoding="utf-8"))
            for sym, entry in cache.items():
                if not isinstance(entry, dict):
                    continue
                sec = entry.get("sector")
                if sec and (out.get(sym, 'Unknown') == 'Unknown'):
                    out[sym] = str(sec).strip()
    except Exception:
        pass
    return out


# ---------- Generic backtest ----------

def run_strategy(strategy_name, params, score_fn, filter_fn=None,
                 start='2008-01-01', end='2026-06-01',
                 init=7000, annual_contrib=7000, log=True, verbose=False,
                 return_holdings=False, contrib_cadence=None):
    """
    params keys:
      n_holdings: int (10-50)
      stock_w_base: float (0.7-0.9) -- target stock allocation; 1-this in BND
      cadence: 'M' / 'Q' / 'A'
      dip_tier: list of (drawdown_threshold, frac_bnd_to_stocks)
      snapback: float -- if stock frac > this, rebalance to base
      sector_cap: float or None
      min_sectors: int or None
      hold_bias: float 0-1 -- if score_rank within top hold_bias*N, keep
      equal_weight: bool

    contrib_cadence: None (legacy = Mar/Jun/Sep/Dec quarterly), or one of
      'M' (every month, /12), 'Q' (Jan/Apr/Jul/Oct, /4), 'A' (Jan, full),
      'none' (no contributions). UI callers should pass an explicit value.
    """
    sigs = compute_monthly_signals()
    ac, dv = load_prices_divs()
    px_m = sigs['price']  # monthly resampled price
    dv_m = dv.resample('BME').sum().reindex(px_m.index).fillna(0)
    etfs = get_etf_series()
    bnd_m = etfs['BND'].resample('BME').last().reindex(px_m.index).ffill()
    spy_m = etfs['SPY'].resample('BME').last().reindex(px_m.index).ffill()

    idx = px_m.index
    idx = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]

    n_hold = params.get('n_holdings', 20)
    stock_w_base = params.get('stock_w_base', 0.80)
    cadence = params.get('cadence', 'Q')
    dip_tier = params.get('dip_tier', [(-0.10, 1/3), (-0.20, 1.0)])
    snapback = params.get('snapback', 0.95)
    sector_cap = params.get('sector_cap', None)
    min_sectors = params.get('min_sectors', None)
    hold_bias = params.get('hold_bias', 0.6)  # keep names in top 60% of new score
    equal_weight = params.get('equal_weight', True)
    bnd_floor = params.get('bnd_floor', 0.10)  # never below this in BND
    # dip_deploy: when dip-buy fires, immediately deploy the freed BND-cash into stocks.
    #   None / False / 'cash' -> legacy behavior (cash sits; monthly sweep redeploys ~stock_w_base of it)
    #   'held_equal'          -> equal-weight buy into currently-held names (same month)
    #   'held_propvalue'      -> buy into currently-held names proportional to current $ value
    #   'rescore'             -> rescore the universe NOW and rebalance to new top-N (mid-cycle rebal)
    #   'scout_only'          -> scan universe, buy up to `scout_n` NEW top-ranked names
    #                            with the freed cash, leave existing holdings alone
    #   'hybrid_scout_eq'     -> split freed cash: half goes to held_equal, half to scout_only
    dip_deploy = params.get('dip_deploy', None)
    scout_n = params.get('scout_n', max(1, params.get('n_holdings', 20) // 4))
    # cumulative_dd: if True, peak does NOT reset after dip fires. Each tier
    # threshold can fire AT MOST ONCE per drawdown; the set of fired tiers
    # clears when SPY recovers to within 1% of the current peak. This lets
    # deeper tiers (-20%, -30%) actually fire on prolonged drawdowns.
    cumulative_dd = params.get('cumulative_dd', False)
    # sector_cap_mode: 'count' (legacy) limits stocks per sector to
    # ceil(n_holdings * sector_cap). 'dollar' enforces sector_cap on
    # dollar weight (sum of target $ in each sector) AT REBALANCE — clips
    # drift winners by selling down sectors over the cap, redirecting slack
    # to BND. With equal_weight=True the two modes pick the same names but
    # 'dollar' also trims winners that drifted above the cap by next rebal.
    sector_cap_mode = params.get('sector_cap_mode', 'count')

    sector_map = (get_enriched_sector_map() if params.get('enrich_sectors')
                  else get_sector_map()) if (sector_cap or min_sectors or dip_deploy in ('scout_only', 'hybrid_scout_eq')) else {}

    # Determine rebal dates
    rebal_months = set()
    if cadence == 'M':
        rebal_months = set(range(1, 13))
    elif cadence == 'Q':
        rebal_months = {3, 6, 9, 12}
    elif cadence == 'A':
        rebal_months = {12}

    # Optional override: allow caller to pick the annual rebal month (research-only)
    if 'rebal_month_override' in params:
        rebal_months = {int(params['rebal_month_override'])}

    # state
    cash = init
    holdings = {}  # ticker -> shares
    sh_bnd = 0.0
    last_peak_spy = float(spy_m.loc[idx[0]])
    dd_tiers_fired = set()  # cumulative_dd: thresholds already fired in current drawdown
    history = []
    holdings_history = {}  # date -> dict copy of holdings
    cashflows = [(idx[0], -init)]
    trades_made = 0

    for i, dt in enumerate(idx):
        force_rescore_this_iter = False
        # Mark-to-market
        v_stocks = 0.0
        for t, sh in holdings.items():
            px = px_m.at[dt, t] if t in px_m.columns else np.nan
            if pd.notna(px):
                v_stocks += sh * float(px)
        # Collect dividends paid this month
        div_paid = 0.0
        for t, sh in holdings.items():
            if t in dv_m.columns:
                d = float(dv_m.at[dt, t])
                if d > 0: div_paid += sh * d
        cash += div_paid

        v_bnd = sh_bnd * float(bnd_m.loc[dt])
        total = v_stocks + v_bnd + cash

        # Contributions
        contrib_amt = 0.0
        if i > 0:
            if contrib_cadence is None:
                # Legacy behavior (preserves all existing callers/sweeps)
                if dt.month % 3 == 0:
                    contrib_amt = annual_contrib / 4
            elif contrib_cadence == 'M':
                contrib_amt = annual_contrib / 12
            elif contrib_cadence == 'Q':
                if dt.month in (1, 4, 7, 10):
                    contrib_amt = annual_contrib / 4
            elif contrib_cadence == 'A':
                if dt.month == 1:
                    contrib_amt = float(annual_contrib)
            # 'none' or unknown -> 0
        if contrib_amt > 0:
            cash += contrib_amt
            cashflows.append((dt, -contrib_amt))
            total += contrib_amt

        # Market dip trigger (use SPY)
        px_spy = float(spy_m.loc[dt])
        last_peak_spy = max(last_peak_spy, px_spy)
        spy_dd = px_spy / last_peak_spy - 1
        # Reset cumulative-DD tier tracking when SPY recovers
        if cumulative_dd and spy_dd >= -0.01 and dd_tiers_fired:
            dd_tiers_fired.clear()
        move_frac = 0
        newly_fired_tiers = []
        for thresh, frac in sorted(dip_tier):  # most negative first
            if spy_dd <= thresh:
                if cumulative_dd and thresh in dd_tiers_fired:
                    continue
                move_frac = max(move_frac, frac)
                newly_fired_tiers.append(thresh)
        # Move bonds -> deploy as cash for next rebal
        if move_frac > 0 and v_bnd > 0:
            move_dollars = v_bnd * move_frac
            # Don't go below floor
            min_bnd = total * bnd_floor
            move_dollars = min(move_dollars, max(0, v_bnd - min_bnd))
            if move_dollars > 0:
                sh_bnd -= move_dollars / float(bnd_m.loc[dt])
                cash += move_dollars
                v_bnd -= move_dollars
                if cumulative_dd:
                    # Record tiers fired; do NOT reset peak (deeper tiers can still fire later)
                    for thr in newly_fired_tiers:
                        dd_tiers_fired.add(thr)
                else:
                    last_peak_spy = px_spy  # reset so we don't keep triggering

                # --- DEPLOY ON DIP: immediately put freed cash into stocks ---
                # Shared decision logic with advisor/engine.py so the live
                # advisor and the backtest harness make identical choices.
                if dip_deploy == 'rescore':
                    force_rescore_this_iter = True
                elif dip_deploy:
                    from advisor.dip_deploy import compute_dip_deploy_buys
                    price_lookup = {
                        t: float(px_m.at[dt, t])
                        for t in px_m.columns
                        if pd.notna(px_m.at[dt, t])
                    }
                    dip_buys = compute_dip_deploy_buys(
                        deploy_mode=dip_deploy,
                        deploy_amt=move_dollars,
                        held_shares=dict(holdings),
                        price_lookup=price_lookup,
                        asof=dt,
                        sigs=sigs,
                        score_fn=score_fn,
                        filter_fn=filter_fn,
                        sector_map=sector_map,
                        params=params,
                    )
                    for tkr, dol, kind in dip_buys:
                        px = price_lookup.get(tkr, 0.0)
                        if px <= 0 or dol <= 0:
                            continue
                        holdings[tkr] = holdings.get(tkr, 0.0) + dol / px
                        cash -= dol
                        v_stocks += dol
                        if kind == 'scout':
                            trades_made += 1

        # Snap-back: if stock_frac > snapback, rebalance to base
        stock_frac = v_stocks / total if total > 0 else 0
        if stock_frac > snapback:
            target_stock = total * stock_w_base
            target_bnd = total * (1 - stock_w_base)
            # Sell stocks proportionally
            scale = target_stock / v_stocks if v_stocks > 0 else 0
            for t in list(holdings.keys()):
                holdings[t] *= scale
            cash += (v_stocks - target_stock)
            v_stocks = target_stock
            # Adjust bnd
            delta_bnd = target_bnd - v_bnd
            if delta_bnd > 0:
                cash -= delta_bnd
                sh_bnd += delta_bnd / float(bnd_m.loc[dt])
            elif delta_bnd < 0:
                sh_bnd += delta_bnd / float(bnd_m.loc[dt])
                cash -= delta_bnd
            v_bnd = sh_bnd * float(bnd_m.loc[dt])

        # Rebalance / select
        if dt.month in rebal_months or i == 0 or force_rescore_this_iter:
            # Score eligible tickers
            scores = score_fn(dt, sigs, params)
            if filter_fn is not None:
                mask = filter_fn(dt, sigs, params)
                scores = scores.where(mask, np.nan)
            scores = scores.dropna()
            if len(scores) < n_hold:
                # not enough -- skip rebalance, keep current
                pass
            else:
                # Sort descending
                top = scores.sort_values(ascending=False)
                # Sector cap & min sectors -- iterative selection
                if sector_cap or min_sectors:
                    picked = []
                    sec_counts = {}
                    per_sec_max = max(1, int(np.ceil(n_hold * (sector_cap or 1.0))))
                    for t in top.index:
                        sec = sector_map.get(t, 'Unknown')
                        if sec_counts.get(sec, 0) < per_sec_max:
                            picked.append(t)
                            sec_counts[sec] = sec_counts.get(sec, 0) + 1
                        if len(picked) >= n_hold:
                            break
                    if min_sectors and len(set(sector_map.get(t,'?') for t in picked)) < min_sectors:
                        # add more sectors at expense of dropping concentrated
                        secs = list(set(sector_map.get(t,'?') for t in top.index))
                        # naive: just trust we have enough sectors usually
                        pass
                    new_names = picked[:n_hold]
                else:
                    new_names = top.head(n_hold).index.tolist()

                # Hold bias: keep currently-held if still in top hold_bias*n_pool
                # AND if they don't violate the sector cap (kept names occupy
                # their sector's slots first; new picks fill remaining slots).
                if hold_bias > 0 and holdings:
                    keep_threshold = int(n_hold / max(hold_bias, 0.01))
                    survivors = set(top.head(keep_threshold).index)
                    kept_candidates = [t for t in holdings if t in survivors]
                    if sector_cap:
                        per_sec_max = max(1, int(np.ceil(n_hold * sector_cap)))
                        kept = []
                        kept_sec_counts: Dict[str, int] = {}
                        for t in kept_candidates:
                            sec = sector_map.get(t, 'Unknown')
                            if kept_sec_counts.get(sec, 0) < per_sec_max:
                                kept.append(t)
                                kept_sec_counts[sec] = kept_sec_counts.get(sec, 0) + 1
                        # Then fill from new_names, skipping any that would push
                        # a sector over the cap given what kept already used.
                        merged_sec_counts = dict(kept_sec_counts)
                        merged = list(kept)
                        for t in new_names:
                            if t in merged:
                                continue
                            sec = sector_map.get(t, 'Unknown')
                            if merged_sec_counts.get(sec, 0) < per_sec_max:
                                merged.append(t)
                                merged_sec_counts[sec] = merged_sec_counts.get(sec, 0) + 1
                            if len(merged) >= n_hold:
                                break
                        final_names = merged[:n_hold]
                    else:
                        final_names = list(dict.fromkeys(kept_candidates + new_names))[:n_hold]
                else:
                    final_names = new_names

                # Build target portfolio: stock_w_base * total in stocks, rest in BND
                # Refresh totals
                v_stocks_cur = sum(holdings[t] * float(px_m.at[dt, t])
                                    for t in holdings if t in px_m.columns and pd.notna(px_m.at[dt, t]))
                v_bnd_cur = sh_bnd * float(bnd_m.loc[dt])
                total_cur = v_stocks_cur + v_bnd_cur + cash
                target_stock_val = total_cur * stock_w_base
                target_bnd_val = total_cur * (1 - stock_w_base)
                if equal_weight:
                    per_name = target_stock_val / len(final_names)
                    target_weights = {t: per_name for t in final_names}
                else:
                    # weight by score (positive scores only, min 0)
                    s_sub = top.reindex(final_names).clip(lower=0)
                    if s_sub.sum() == 0:
                        per_name = target_stock_val / len(final_names)
                        target_weights = {t: per_name for t in final_names}
                    else:
                        target_weights = {t: target_stock_val * (s_sub[t]/s_sub.sum()) for t in final_names}

                # DOLLAR-WEIGHTED SECTOR CAP: scale down any sector whose total
                # target $ exceeds sector_cap * total portfolio, redirecting the
                # slack to BND. This is the key drift-fix — even after a clean
                # equal-weight rebalance, winners between rebals can push a
                # sector above the cap. Trimming here keeps the next portfolio
                # under the cap by construction.
                if sector_cap_mode == 'dollar' and sector_cap and target_weights:
                    cap_dollars = sector_cap * total_cur
                    sec_totals: Dict[str, float] = {}
                    for t in target_weights:
                        sec = sector_map.get(t, 'Unknown')
                        sec_totals[sec] = sec_totals.get(sec, 0.0) + target_weights[t]
                    slack = 0.0
                    for sec, sec_tot in sec_totals.items():
                        if sec_tot > cap_dollars + 1e-6:
                            scale = cap_dollars / sec_tot
                            for t in list(target_weights):
                                if sector_map.get(t, 'Unknown') == sec:
                                    reduction = target_weights[t] * (1 - scale)
                                    target_weights[t] *= scale
                                    slack += reduction
                    target_bnd_val += slack

                # Sell stocks not in target
                for t in list(holdings.keys()):
                    if t not in target_weights:
                        px = float(px_m.at[dt, t]) if t in px_m.columns and pd.notna(px_m.at[dt, t]) else 0
                        cash += holdings[t] * px
                        del holdings[t]
                        trades_made += 1
                # Adjust existing & buy new
                for t, tgt_val in target_weights.items():
                    if t not in px_m.columns or pd.isna(px_m.at[dt, t]): continue
                    px = float(px_m.at[dt, t])
                    cur_val = holdings.get(t, 0) * px
                    delta = tgt_val - cur_val
                    if abs(delta) < 0.01 * tgt_val: continue
                    # buy/sell shares
                    delta_sh = delta / px
                    holdings[t] = holdings.get(t, 0) + delta_sh
                    cash -= delta
                    trades_made += 1
                # Adjust BND
                px_bnd = float(bnd_m.loc[dt])
                cur_bnd = sh_bnd * px_bnd
                delta_b = target_bnd_val - cur_bnd
                if abs(delta_b) > 0.01:
                    sh_bnd += delta_b / px_bnd
                    cash -= delta_b

        # Deploy any leftover cash into existing stocks proportionally
        if cash > total * 0.005 and holdings:
            v_stocks_now = sum(holdings[t] * float(px_m.at[dt, t])
                               for t in holdings if t in px_m.columns and pd.notna(px_m.at[dt, t]))
            if v_stocks_now > 0:
                for t in list(holdings.keys()):
                    if t in px_m.columns and pd.notna(px_m.at[dt, t]):
                        px = float(px_m.at[dt, t])
                        cur_v = holdings[t] * px
                        add = cash * (cur_v / v_stocks_now) * stock_w_base
                        holdings[t] += add / px
                # Add to BND
                add_bnd = cash * (1 - stock_w_base)
                sh_bnd += add_bnd / float(bnd_m.loc[dt])
                cash = 0

        # Final mark-to-market
        v_stocks = sum(holdings[t] * float(px_m.at[dt, t])
                       for t in holdings if t in px_m.columns and pd.notna(px_m.at[dt, t]))
        v_bnd = sh_bnd * float(bnd_m.loc[dt])
        total = v_stocks + v_bnd + cash
        history.append((dt, total, len(holdings)))
        holdings_history[dt] = dict(holdings)

    eq = pd.Series([h[1] for h in history], index=[h[0] for h in history])
    cashflows.append((idx[-1], eq.iloc[-1]))
    m = metrics(eq, cashflows)
    m['trades'] = trades_made
    m['final_holdings'] = len(holdings)
    m['strategy'] = strategy_name
    m['params'] = {k: (list(v) if isinstance(v, (list,tuple)) else v)
                    for k, v in params.items()}
    m['window'] = (str(idx[0].date()), str(idx[-1].date()))
    if log:
        log_result(strategy_name, params, m)
    if verbose:
        print(f'{strategy_name}: IRR={m["irr"]*100:.2f}%  DD={m["maxdd"]*100:.1f}%  Calmar={m["calmar"]:.2f}  final=${m["final"]:,.0f}  trades={trades_made}')
    if return_holdings:
        return eq, cashflows, m, holdings_history
    return eq, cashflows, m


def log_result(strategy_name, params, m):
    rec = {
        'strategy': strategy_name,
        'window': m['window'],
        'irr': m.get('irr'),
        'maxdd': m.get('maxdd'),
        'calmar': m.get('calmar'),
        'final': m.get('final'),
        'multiple': m.get('multiple'),
        'trades': m.get('trades'),
        'final_holdings': m.get('final_holdings'),
        'params': m.get('params'),
        'ts': time.time(),
    }
    with open(RESULTS_FILE, 'a') as f:
        f.write(json.dumps(rec, default=str) + '\n')
