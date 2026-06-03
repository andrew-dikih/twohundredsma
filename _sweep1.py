"""Strategy sweep: generate many strategies parametrically and run them.

We define a small set of orthogonal SCORE primitives, then sweep over:
  - score weights
  - n_holdings (10/15/20/30/50)
  - cadence (Q/A; some M)
  - stock_w_base (0.70/0.80/0.90)
  - dip_tier variants
  - sector_cap on/off
  - hold_bias 0.4/0.6/0.8

Logs every result to strat_results/results.jsonl. Prints a leaderboard at end.
"""
import json
import time
import numpy as np
import pandas as pd
import itertools
from _harness import run_strategy
from _signal_engine import compute_monthly_signals, benchmark_sso_bnd, metrics

# ---------- Score builders ----------

def make_score(weights):
    """weights: dict of signal_name -> (direction, weight). direction='+' or '-'.
    Signals available: div_yield, dist_200w, vol_12m, vol_36m, ret_12m, ret_36m,
                       dd_3y, dd_5y, div_yield_5y, div_consistency, div_growth_5y
    All scores converted to percentile ranks per date.
    """
    def score(date, sigs, ctx):
        composite = None
        total_w = 0
        for sname, (direction, w) in weights.items():
            if sname not in sigs:
                continue
            try:
                s = sigs[sname].loc[date]
            except KeyError:
                continue
            if direction == '+':
                r = s.rank(pct=True)
            else:
                r = (-s).rank(pct=True)
            if composite is None:
                composite = r * w
            else:
                composite = composite + r * w
            total_w += w
        return composite if composite is not None else pd.Series(dtype=float)
    return score


# ---------- Filter builders ----------

def make_filter(min_years=5, min_div_months=12, require_positive_dy=False):
    def filt(date, sigs, ctx):
        px = sigs['price']
        dv_cons = sigs['div_consistency']
        dy = sigs['div_yield']
        cutoff = date - pd.DateOffset(years=min_years)
        try:
            past_idx = px.index[px.index <= cutoff][-1]
        except IndexError:
            return pd.Series(False, index=px.columns)
        m = px.loc[past_idx].notna() & px.loc[date].notna()
        if min_div_months > 0:
            m = m & (dv_cons.loc[date] >= min_div_months)
        if require_positive_dy:
            m = m & (dy.loc[date] > 0)
        return m
    return filt


# ---------- Strategy definitions ----------

STRATEGIES = {}

# Base scoring primitives (different "flavors" of value)
SCORE_VARIANTS = {
    'div_value': {'div_yield': ('+', 0.35), 'dist_200w': ('-', 0.25), 'vol_12m': ('-', 0.20), 'ret_12m': ('+', 0.20)},
    'pure_div':  {'div_yield': ('+', 0.50), 'div_yield_5y': ('+', 0.30), 'div_consistency': ('+', 0.20)},
    'low_vol':   {'vol_12m': ('-', 0.50), 'vol_36m': ('-', 0.30), 'ret_36m': ('+', 0.20)},
    'mean_rev':  {'dd_3y': ('+', 0.50), 'dist_200w': ('-', 0.30), 'div_yield': ('+', 0.20)},  # dd_3y is negative so '+'-direction means least negative... wait
    'deep_value':{'dd_5y': ('+', 0.40), 'dist_200w': ('-', 0.20), 'div_yield': ('+', 0.30), 'ret_36m': ('+', 0.10)},
    'momentum':  {'ret_12m': ('+', 0.50), 'ret_36m': ('+', 0.30), 'vol_12m': ('-', 0.20)},
    'qual_value':{'div_yield': ('+', 0.30), 'div_growth_5y': ('+', 0.20), 'vol_12m': ('-', 0.20), 'ret_36m': ('+', 0.30)},
    'income_stab':{'div_yield_5y': ('+', 0.40), 'div_consistency': ('+', 0.30), 'vol_36m': ('-', 0.30)},
    'dvm':       {'div_yield': ('+', 0.40), 'ret_12m': ('+', 0.30), 'vol_12m': ('-', 0.30)},
    'antifrag':  {'dd_3y': ('+', 0.30), 'div_yield': ('+', 0.30), 'vol_12m': ('-', 0.20), 'ret_36m': ('+', 0.20)},
    'div_growth':{'div_growth_5y': ('+', 0.40), 'div_yield': ('+', 0.30), 'ret_36m': ('+', 0.30)},
    'momhi_div': {'ret_12m': ('+', 0.40), 'div_yield': ('+', 0.30), 'div_consistency': ('+', 0.30)},
}

# Note: my "direction" convention: '+' = larger raw value is better.
# For dd_3y (which is <=0), '+' means least negative = least drawn-down currently.
# So if you want to BUY discounted (most negative dd), use '-'.
# Fix the mean_rev / deep_value / antifrag scoring:
SCORE_VARIANTS['mean_rev']  = {'dd_3y': ('-', 0.50), 'dist_200w': ('-', 0.30), 'div_yield': ('+', 0.20)}
SCORE_VARIANTS['deep_value']= {'dd_5y': ('-', 0.40), 'dist_200w': ('-', 0.20), 'div_yield': ('+', 0.30), 'ret_36m': ('+', 0.10)}
SCORE_VARIANTS['antifrag']  = {'dd_3y': ('-', 0.30), 'div_yield': ('+', 0.30), 'vol_12m': ('-', 0.20), 'ret_36m': ('+', 0.20)}


def main():
    # Reset results file
    import os
    if os.path.exists('strat_results/results.jsonl'):
        os.rename('strat_results/results.jsonl', f'strat_results/results_archive_{int(time.time())}.jsonl')

    sigs = compute_monthly_signals()  # warm cache

    # Compute benchmark once per window
    print('Benchmarks:')
    bench = {}
    for w, (s, e) in [('18y', ('2008-01-01','2026-06-01')),
                      ('13y', ('2013-01-01','2026-06-01')),
                      ('8y',  ('2018-01-01','2026-06-01'))]:
        eq, cf = benchmark_sso_bnd(s, e)
        m = metrics(eq, cf)
        bench[w] = m
        print(f'  SSO/BND {w}: IRR={m["irr"]*100:.2f}%  DD={m["maxdd"]*100:.1f}%  Calmar={m["calmar"]:.2f}  final=${m["final"]:,.0f}')

    # Sweep params
    n_holdings_list  = [15, 20, 30]
    stock_w_list     = [0.75, 0.85]
    cadence_list     = ['Q', 'A']
    sector_cap_list  = [None, 0.20]
    hold_bias_list   = [0.5, 0.8]
    dip_variants = {
        'none':    [],
        'mod':     [(-0.10, 1/3), (-0.20, 1.0)],
        'aggr':    [(-0.05, 1/3), (-0.10, 1/2), (-0.20, 1.0)],
    }
    min_div_months = 12  # need div paid in 12+/60 last months
    min_years = 5

    filt = make_filter(min_years=min_years, min_div_months=min_div_months)

    total = (len(SCORE_VARIANTS) * len(n_holdings_list) * len(stock_w_list)
             * len(cadence_list) * len(sector_cap_list) * len(hold_bias_list) * len(dip_variants))
    print(f'\nSweeping {total} strategies (18y window)...')

    t0 = time.time()
    count = 0
    failures = 0
    for score_name, weights in SCORE_VARIANTS.items():
        for nh, sw, cad, secap, hb, (dipname, diptier) in itertools.product(
            n_holdings_list, stock_w_list, cadence_list,
            sector_cap_list, hold_bias_list, dip_variants.items()
        ):
            name = f'{score_name}_n{nh}_sw{int(sw*100)}_{cad}_sec{secap}_hb{int(hb*100)}_dip{dipname}'
            params = {
                'n_holdings': nh,
                'stock_w_base': sw,
                'cadence': cad,
                'sector_cap': secap,
                'min_sectors': 7 if secap else None,
                'hold_bias': hb,
                'dip_tier': diptier,
                'snapback': 0.95,
                'equal_weight': True,
                'bnd_floor': 0.05,
            }
            score_fn = make_score(weights)
            try:
                eq, cf, m = run_strategy(name, params, score_fn, filt,
                                          start='2008-01-01', end='2026-06-01',
                                          verbose=False)
                count += 1
                if count % 20 == 0:
                    rate = count / (time.time()-t0)
                    eta = (total - count) / max(rate, 0.01)
                    print(f'  {count}/{total}  rate={rate:.2f}/s  ETA={eta/60:.1f}min  last: {name[:50]} IRR={m["irr"]*100:.1f}%')
            except Exception as e:
                failures += 1
                print(f'  FAIL {name}: {e}')

    print(f'\nDone: {count} runs, {failures} failures, {(time.time()-t0)/60:.1f}min')


if __name__ == '__main__':
    main()
