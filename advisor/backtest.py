"""Backtest engine for the /backtest UI page.

Strategy registry + dispatcher producing equity series + metrics in a uniform
shape so a single dual-axis chart can overlay any subset.

All UI-driven backtests pass log=False so they NEVER pollute the research
leaderboard (strat_results/results.jsonl).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from advisor.engine import CHAMPION_PARAMS


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

# Champion's dip_deploy comes from the live engine so the label always matches
# what the advisor actually runs.
_CHAMP_DD = CHAMPION_PARAMS["dip_deploy"]
_OTHER_DDS = [dd for dd in ("held_equal", "scout_only", "hybrid_scout_eq") if dd != _CHAMP_DD]

# 3 stock strategies (champion + 2 alternate dip-deploy variants) + 3 ETF benchmark mixes.
STRATEGIES: Dict[str, Dict[str, Any]] = {
    "pure_div_champion": {
        "label": f"Pure-Div Champion ({_CHAMP_DD} dip-deploy)",
        "kind": "stock",
        "score_variant": "pure_div",
        "dip_deploy": _CHAMP_DD,
    },
    f"pure_div_{_OTHER_DDS[0]}": {
        "label": f"Pure-Div + {_OTHER_DDS[0]} dip-deploy",
        "kind": "stock",
        "score_variant": "pure_div",
        "dip_deploy": _OTHER_DDS[0],
    },
    f"pure_div_{_OTHER_DDS[1]}": {
        "label": f"Pure-Div + {_OTHER_DDS[1]} dip-deploy",
        "kind": "stock",
        "score_variant": "pure_div",
        "dip_deploy": _OTHER_DDS[1],
    },
    "sso_bnd_70_30": {
        "label": "SSO/BND 70/30",
        "kind": "etf_mix",
        "weights": {"SSO": 0.70, "BND": 0.30},
    },
    "vti_bnd_70_30": {
        "label": "VTI/BND 70/30",
        "kind": "etf_mix",
        "weights": {"VTI": 0.70, "BND": 0.30},
    },
    "sso_vti_bnd_50_30_20": {
        "label": "SSO/VTI/BND 50/30/20",
        "kind": "etf_mix",
        "weights": {"SSO": 0.50, "VTI": 0.30, "BND": 0.20},
    },
}

VALID_CADENCES = ("M", "Q", "A", "none")
MAX_STRATEGIES_PER_RUN = 6


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _contribution_months(cadence: str) -> set:
    if cadence == "M":
        return set(range(1, 13))
    if cadence == "Q":
        return {1, 4, 7, 10}
    if cadence == "A":
        return {1}
    return set()  # 'none'


def _per_period_amount(annual: float, cadence: str) -> float:
    if cadence == "M":
        return annual / 12.0
    if cadence == "Q":
        return annual / 4.0
    if cadence == "A":
        return float(annual)
    return 0.0


def _window_dates(years: int) -> Tuple[str, str]:
    """End = today; start = today - `years` years (clamped to ISO)."""
    end = date.today()
    try:
        start = end.replace(year=end.year - int(years))
    except ValueError:  # Feb 29 in non-leap target
        start = end.replace(month=2, day=28, year=end.year - int(years))
    return start.isoformat(), end.isoformat()


def _equity_to_points(eq: pd.Series) -> List[Dict[str, Any]]:
    return [
        {"date": str(idx.date()), "value": round(float(v), 2)}
        for idx, v in eq.items()
    ]


# ---------------------------------------------------------------------------
# Stock strategy backtest (wraps _harness.run_strategy)
# ---------------------------------------------------------------------------

# Champion params (copied from advisor.engine.CHAMPION_PARAMS to avoid an import
# cycle and let us swap dip_deploy per variant).
_BASE_STOCK_PARAMS: Dict[str, Any] = dict(
    n_holdings=20,
    stock_w_base=0.85,
    cadence="A",
    sector_cap=0.20,
    sector_cap_mode="dollar",
    enrich_sectors=True,
    min_sectors=7,
    hold_bias=0.5,
    dip_tier=[(-0.05, 1 / 3), (-0.10, 1 / 2), (-0.20, 1.0)],
    snapback=0.95,
    equal_weight=True,
    bnd_floor=0.05,
    rebal_month_override=2,
)


def _run_stock(spec: Dict[str, Any], start: str, end: str, init: float,
               annual_contrib: float, contrib_cadence: str) -> Dict[str, Any]:
    # Late imports keep page load fast for users who don't visit /backtest.
    from _harness import run_strategy
    from _sweep1 import SCORE_VARIANTS, make_filter, make_score

    weights = SCORE_VARIANTS[spec["score_variant"]]
    score_fn = make_score(weights)
    filter_fn = make_filter(min_years=5, min_div_months=12)

    params = dict(_BASE_STOCK_PARAMS)
    params["dip_deploy"] = spec.get("dip_deploy")

    eq, cashflows, m = run_strategy(
        spec["label"], params, score_fn, filter_fn,
        start=start, end=end,
        init=float(init), annual_contrib=float(annual_contrib),
        log=False, verbose=False,
        contrib_cadence=contrib_cadence,
    )
    return {
        "series": _equity_to_points(eq),
        "metrics": m,
    }


# ---------------------------------------------------------------------------
# ETF mix backtest (mirrors /benchmark rules but is calendar-driven, not
# anchored to actual run dates)
# ---------------------------------------------------------------------------

REBAL_MONTH = 2  # February — matches champion strategy's annual rebal month


def _run_etf_mix(spec: Dict[str, Any], start: str, end: str, init: float,
                 annual_contrib: float, contrib_cadence: str) -> Dict[str, Any]:
    from _signal_engine import get_etf_series, metrics

    weights: Dict[str, float] = dict(spec["weights"])
    etfs = get_etf_series()

    # Use month-end series so all tickers share a uniform monthly grid.
    series = {t: etfs[t].resample("BME").last() for t in weights}
    # Intersect indexes so we have a price for every ticker at every step.
    common_index: Optional[pd.DatetimeIndex] = None
    for s in series.values():
        common_index = s.index if common_index is None else common_index.intersection(s.index)
    assert common_index is not None and len(common_index) > 0

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    idx = common_index[(common_index >= start_ts) & (common_index <= end_ts)]
    if len(idx) == 0:
        return {"series": [], "metrics": {}}

    contrib_months = _contribution_months(contrib_cadence)
    contrib_amt = _per_period_amount(annual_contrib, contrib_cadence)

    # Seed: at idx[0] buy each ticker at its month-end price.
    seed_prices = {t: float(series[t].loc[idx[0]]) for t in weights}
    pos: Dict[str, float] = {t: (init * w) / seed_prices[t] for t, w in weights.items()}
    cashflows: List[Tuple[pd.Timestamp, float]] = [(idx[0], -float(init))]
    history: List[Tuple[pd.Timestamp, float]] = []

    prev_prices = dict(seed_prices)
    for i, dt in enumerate(idx):
        # Mark to market with this month's prices
        cur_prices = {t: float(series[t].loc[dt]) for t in weights}
        total = sum(pos[t] * cur_prices[t] for t in weights)

        # Contributions (skip seed step)
        if i > 0 and dt.month in contrib_months and contrib_amt > 0:
            for t, w in weights.items():
                pos[t] += (contrib_amt * w) / cur_prices[t]
            cashflows.append((dt, -float(contrib_amt)))
            total += contrib_amt

        # Annual rebalance in REBAL_MONTH (skip first month)
        if i > 0 and dt.month == REBAL_MONTH:
            for t, w in weights.items():
                pos[t] = (total * w) / cur_prices[t]

        # Record portfolio value AFTER contributions/rebal (matches harness order)
        final_total = sum(pos[t] * cur_prices[t] for t in weights)
        history.append((dt, final_total))
        prev_prices = cur_prices

    eq = pd.Series([h[1] for h in history], index=[h[0] for h in history])
    cashflows.append((idx[-1], float(eq.iloc[-1])))
    m = metrics(eq, cashflows)
    return {"series": _equity_to_points(eq), "metrics": m}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_backtest_set(
    strategy_ids: List[str],
    years: int,
    init: float,
    annual_contrib: float,
    contrib_cadence: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Run all selected strategies and return (results_by_id, run_info).

    run_info contains the resolved window dates + total contributions for the
    summary banner.
    """
    if contrib_cadence not in VALID_CADENCES:
        raise ValueError(f"contrib_cadence must be one of {VALID_CADENCES}")
    if not strategy_ids:
        raise ValueError("Select at least one strategy")
    if len(strategy_ids) > MAX_STRATEGIES_PER_RUN:
        raise ValueError(f"At most {MAX_STRATEGIES_PER_RUN} strategies per run")
    unknown = [s for s in strategy_ids if s not in STRATEGIES]
    if unknown:
        raise ValueError(f"Unknown strategy ids: {unknown}")

    start, end = _window_dates(int(years))
    results: Dict[str, Dict[str, Any]] = {}
    for sid in strategy_ids:
        spec = STRATEGIES[sid]
        runner = _run_stock if spec["kind"] == "stock" else _run_etf_mix
        out = runner(spec, start, end, float(init), float(annual_contrib), contrib_cadence)
        results[sid] = {
            "id": sid,
            "label": spec["label"],
            "kind": spec["kind"],
            "series": out["series"],
            "metrics": out["metrics"],
        }

    # Total contributed across the window (skip seed: contributions begin period 1)
    contrib_amt = _per_period_amount(annual_contrib, contrib_cadence)
    contrib_months = _contribution_months(contrib_cadence)
    # Count occurrences of contrib months in (start, end] excluding start month
    months_between = pd.date_range(start=start, end=end, freq="MS")
    periods = sum(1 for d in months_between[1:] if d.month in contrib_months)
    total_contributed = float(init) + periods * contrib_amt

    # Cumulative-contributed at each chart date (union of all series dates).
    # Used by the chart's right axis so the % shown is true investment return
    # (excludes contributions, which would otherwise inflate the curve).
    all_dates = sorted({p["date"] for r in results.values() for p in r["series"]})
    cum_by_date: Dict[str, float] = {}
    running = float(init)
    for i, ds in enumerate(all_dates):
        if i > 0:
            month = int(ds[5:7])
            if month in contrib_months:
                running += contrib_amt
        cum_by_date[ds] = round(running, 2)

    run_info = {
        "start": start,
        "end": end,
        "years": int(years),
        "init": float(init),
        "annual_contrib": float(annual_contrib),
        "contrib_cadence": contrib_cadence,
        "total_contributed": round(total_contributed, 2),
        "periods": periods,
        "cum_contrib_by_date": cum_by_date,
    }
    return results, run_info
