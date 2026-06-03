"""Signal engine + multi-stock backtest harness.

Loads price/dividend data, computes monthly signals per ticker, and provides
a generic backtest function that takes a `pick_fn(date, available_tickers, signals_df) -> list[(ticker,weight)]`.

Designed to be imported by strategy sweep scripts.

Benchmark: SSO 70/30 BND monthly tierA dip-buy (champion from prior work).
"""
import pickle
import pandas as pd
import numpy as np
from datetime import datetime

DATA_DIV = 'dat/stock_data_longdiv_2026-06-01.pkl'
SSO_PKL  = 'dat/sso_long.pkl'
BND_PKL  = 'dat/bnd_long.pkl'
VTI_PKL  = 'dat/vti_long.pkl'
SPY_PKL  = 'dat/spy_long.pkl'

# ---------- Data loaders ----------

_CACHE = {}

def load_prices_divs():
    """Return (adj_close_wide, div_wide) DataFrames indexed by date, columns=tickers."""
    if 'prices' in _CACHE:
        return _CACHE['prices'], _CACHE['divs']
    df = pickle.load(open(DATA_DIV, 'rb'))
    ac_cols = [c for c in df.columns if c.startswith('Adj Close__')]
    dv_cols = [c for c in df.columns if c.startswith('Dividends__')]
    ac = df[ac_cols].copy()
    ac.columns = [c.split('__', 1)[1] for c in ac_cols]
    dv = df[dv_cols].copy()
    dv.columns = [c.split('__', 1)[1] for c in dv_cols]
    # Align div index to ac
    dv = dv.reindex(ac.index).fillna(0)
    _CACHE['prices'] = ac
    _CACHE['divs'] = dv
    return ac, dv


def load_etf(path):
    s = pickle.load(open(path, 'rb'))
    if isinstance(s, pd.DataFrame):
        # MultiIndex columns like ('Close','SSO') -- prefer Close
        if isinstance(s.columns, pd.MultiIndex):
            # take Close column for the single ticker
            if 'Close' in s.columns.get_level_values(0):
                s = s['Close']
                # might still be DataFrame with one column
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0]
        else:
            for c in ['Adj Close', 'adjusted_close', 'Close']:
                if c in s.columns:
                    s = s[c]
                    break
    return s


def get_etf_series():
    return {
        'SSO': load_etf(SSO_PKL),
        'BND': load_etf(BND_PKL),
        'VTI': load_etf(VTI_PKL),
        'SPY': load_etf(SPY_PKL),
    }


# ---------- Signal engine (monthly) ----------

def compute_monthly_signals():
    """Return dict of wide DataFrames (date x ticker) for each signal."""
    if 'signals' in _CACHE:
        return _CACHE['signals']
    ac, dv = load_prices_divs()
    # Monthly resample (BME = business month end)
    px_m = ac.resample('BME').last()
    # Sum divs over month, then trailing 12m
    dv_m = dv.resample('BME').sum()

    # total return monthly = (px + div) / prev_px - 1
    px_prev = px_m.shift(1)
    tot_ret = (px_m + dv_m) / px_prev - 1
    # log returns for vol
    log_ret = np.log(1 + tot_ret)

    sig = {}
    sig['price']   = px_m
    sig['ret_1m']  = tot_ret
    sig['ret_12m'] = (1 + tot_ret).rolling(12).apply(np.prod, raw=True) - 1
    sig['ret_36m'] = (1 + tot_ret).rolling(36).apply(np.prod, raw=True) - 1
    sig['ret_60m'] = (1 + tot_ret).rolling(60).apply(np.prod, raw=True) - 1
    sig['vol_12m'] = log_ret.rolling(12).std() * np.sqrt(12)
    sig['vol_36m'] = log_ret.rolling(36).std() * np.sqrt(12)
    # drawdown from rolling 36/60 mo peak (price-only is fine for "discount" feel)
    roll36 = px_m.rolling(36, min_periods=12).max()
    roll60 = px_m.rolling(60, min_periods=24).max()
    sig['dd_3y'] = px_m / roll36 - 1  # negative = below peak
    sig['dd_5y'] = px_m / roll60 - 1
    # 200-week SMA in monthly = ~46 months
    sma_200w = px_m.rolling(46, min_periods=24).mean()
    sig['dist_200w'] = px_m / sma_200w - 1
    # trailing 12m div yield
    div_ttm = dv_m.rolling(12).sum()
    sig['div_yield'] = div_ttm / px_m
    # 5y div yield
    div_5y = dv_m.rolling(60).sum() / 5
    sig['div_yield_5y'] = div_5y / px_m
    # consistent dividend payer: months in last 12 with any div > 0 (proxy)
    div_months = (dv_m > 0).rolling(60).sum()
    sig['div_consistency'] = div_months  # how many of last 60mo had a div
    # div growth proxy: 5y div / prior 5y div
    div_5y_prior = dv_m.rolling(60).sum().shift(60)
    sig['div_growth_5y'] = (dv_m.rolling(60).sum() / div_5y_prior - 1).replace([np.inf, -np.inf], np.nan)

    # composite "value+income" score (computed per-date as rank)
    # Strategies can build their own composites; we just provide raw signals.

    _CACHE['signals'] = sig
    return sig


# ---------- Benchmark: SSO 70/30 BND monthly tierA dip-buy ----------

def benchmark_sso_bnd(start, end, init=7000, annual_contrib=7000):
    """Replicate champion: SSO 70 / BND 30, monthly, tierA dip-buy on SSO drawdown.
    Returns (equity_series, cashflows_list).
    cashflows: list of (date, signed_amount). Negative = invested, last positive = final value.
    """
    etfs = get_etf_series()
    sso = etfs['SSO'].resample('BME').last().dropna()
    bnd = etfs['BND'].resample('BME').last().dropna()
    idx = sso.index.intersection(bnd.index)
    idx = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]
    sso = sso.reindex(idx); bnd = bnd.reindex(idx)

    base_stock_w = 0.70
    quarterly_contrib = annual_contrib / 4
    cash = init
    sh_sso = 0.0; sh_bnd = 0.0
    last_peak_sso = float(sso.iloc[0])
    history = []
    cashflows = [(idx[0], -init)]

    for i, dt in enumerate(idx):
        px_sso = float(sso.iloc[i]); px_bnd = float(bnd.iloc[i])
        if i == 0:
            sh_sso = (init * base_stock_w) / px_sso
            sh_bnd = (init * (1-base_stock_w)) / px_bnd
            cash = 0
            history.append((dt, init, base_stock_w))
            continue

        if dt.month % 3 == 0:
            cash += quarterly_contrib
            cashflows.append((dt, -quarterly_contrib))

        last_peak_sso = max(last_peak_sso, px_sso)
        dd = px_sso / last_peak_sso - 1

        v_sso = sh_sso * px_sso
        v_bnd = sh_bnd * px_bnd
        total = v_sso + v_bnd + cash
        stock_frac = v_sso / total if total > 0 else 0

        move_frac = 0
        if dd <= -0.15: move_frac = 1.0
        elif dd <= -0.10: move_frac = 0.5
        elif dd <= -0.05: move_frac = 1/3
        if move_frac > 0 and v_bnd > 0:
            move_dollars = v_bnd * move_frac
            sh_bnd -= move_dollars / px_bnd
            sh_sso += move_dollars / px_sso
            v_sso = sh_sso * px_sso; v_bnd = sh_bnd * px_bnd
            total = v_sso + v_bnd + cash
            stock_frac = v_sso/total if total>0 else 0
            last_peak_sso = px_sso

        if stock_frac > 0.95:
            target_sso = total * base_stock_w
            target_bnd = total * (1-base_stock_w)
            sh_sso = target_sso / px_sso
            sh_bnd = target_bnd / px_bnd
            cash = 0
            v_sso = target_sso; v_bnd = target_bnd

        if cash > 0.01:
            v_sso += cash * stock_frac
            v_bnd += cash * (1 - stock_frac)
            sh_sso = v_sso / px_sso
            sh_bnd = v_bnd / px_bnd
            cash = 0
            total = v_sso + v_bnd

        history.append((dt, total, v_sso/total if total>0 else 0))

    eq = pd.Series([h[1] for h in history], index=[h[0] for h in history])
    cashflows.append((idx[-1], eq.iloc[-1]))
    return eq, cashflows


# ---------- Metrics ----------

def metrics(equity, cashflows=None):
    """If cashflows given, computes IRR (money-weighted); also reports terminal/total_contrib/multiple.
    Always reports MaxDD (peak-to-trough on equity), simple CAGR, Calmar (IRR-based if cashflows).
    """
    n_years = (equity.index[-1] - equity.index[0]).days / 365.25
    final = equity.iloc[-1]
    init = equity.iloc[0]
    cagr_simple = (final / init) ** (1/n_years) - 1 if n_years > 0 and init > 0 else 0
    roll_max = equity.cummax()
    dd = equity / roll_max - 1
    maxdd = dd.min()
    out = {
        'final': final, 'init': init, 'years': n_years,
        'cagr_simple': cagr_simple, 'maxdd': maxdd,
    }
    if cashflows:
        irr = money_weighted_irr(cashflows)
        total_contrib = sum(-c[1] for c in cashflows[:-1])
        out['total_contrib'] = total_contrib
        out['irr'] = irr
        out['multiple'] = final / total_contrib if total_contrib > 0 else np.nan
        out['calmar'] = -irr/maxdd if maxdd<0 and not np.isnan(irr) else np.nan
    else:
        out['calmar'] = -cagr_simple/maxdd if maxdd<0 else np.nan
    return out


def money_weighted_irr(cashflows):
    import scipy.optimize as opt
    dates = [c[0] for c in cashflows]
    amts = np.array([c[1] for c in cashflows], dtype=float)
    t0 = dates[0]
    years = np.array([(d - t0).days / 365.25 for d in dates])
    def npv(r):
        return np.sum(amts / (1+r) ** years)
    try:
        return opt.brentq(npv, -0.99, 10)
    except Exception:
        try:
            return opt.brentq(npv, -0.5, 2)
        except Exception:
            return np.nan


if __name__ == '__main__':
    print('Loading data...')
    ac, dv = load_prices_divs()
    print(f'Price shape: {ac.shape}, divs shape: {dv.shape}')
    print('Computing signals...')
    sig = compute_monthly_signals()
    print(f'Signals: {list(sig.keys())}')
    print(f'Each signal shape: {sig["price"].shape}')
    print('Benchmark SSO/BND 2008-2026...')
    eq, cf = benchmark_sso_bnd('2008-01-01', '2026-06-01')
    m = metrics(eq, cf)
    print(f'  IRR={m["irr"]*100:.2f}%  MaxDD={m["maxdd"]*100:.1f}%  Calmar={m["calmar"]:.2f}  final=${m["final"]:,.0f}  contrib=${m["total_contrib"]:,.0f}  mult={m["multiple"]:.2f}x')
    print('Benchmark SSO/BND 2013-2026 (13y)...')
    eq, cf = benchmark_sso_bnd('2013-01-01', '2026-06-01')
    m = metrics(eq, cf)
    print(f'  IRR={m["irr"]*100:.2f}%  MaxDD={m["maxdd"]*100:.1f}%  Calmar={m["calmar"]:.2f}  final=${m["final"]:,.0f}  contrib=${m["total_contrib"]:,.0f}  mult={m["multiple"]:.2f}x')
