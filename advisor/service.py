"""Service layer: ties parser + engine + state + AI review together.

Provides a single `run_advisor(portfolio, force=False)` entry point that the
FastAPI app calls. Handles:
  - Loading market data (signals + price lookups + sector map)
  - Resolving last_peak_spy from state (or initializing it)
  - Quarterly contribution detection + ledger upsert
  - Calling engine.evaluate()
  - Calling ai_review.review() (graceful)
  - Persisting the run record
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from advisor.engine import CHAMPION_PARAMS, EvaluationResult, evaluate
from advisor.parser import Portfolio
from advisor.state import (
    DEFAULT_ACCOUNT,
    RunRecord,
    Store,
    is_contribution_month,
    quarter_of,
)


@dataclass
class AdvisorRunOptions:
    enable_ai: bool = True              # set False to skip AI review entirely
    contribution_quarterly: float = 1750.0
    force_ignore_cadence: bool = False  # bypass < 25-day pushback
    force_rebalance: bool = False       # treat this run as the annual rebalance month
    min_days_between_runs: int = 25
    account: str = DEFAULT_ACCOUNT


@dataclass
class AdvisorRunResult:
    eval: EvaluationResult
    ai_review: Optional[Dict[str, Any]]
    ai_status: str                       # 'full' | 'partial' | 'disabled' | 'failed'
    ai_error: Optional[str]
    run_id: int
    contribution_due: float
    contribution_quarter: Optional[str]
    pushback_reason: Optional[str] = None  # if cadence violation


# ---- Data loading (cached singletons) ----

_MARKET_CACHE: Dict[str, Any] = {}


def _load_market_data() -> Tuple[Dict[str, pd.DataFrame], Dict[str, str], Dict[str, Any]]:
    """Returns (signals, sector_map, etfs_dict)."""
    if "loaded" in _MARKET_CACHE:
        return _MARKET_CACHE["sigs"], _MARKET_CACHE["sector_map"], _MARKET_CACHE["etfs"]
    # Lazy import so non-advisor code paths aren't slowed
    from _signal_engine import compute_monthly_signals, get_etf_series
    from _harness import get_enriched_sector_map
    sigs = compute_monthly_signals()
    sector_map = get_enriched_sector_map()
    etfs = get_etf_series()
    _MARKET_CACHE["sigs"] = sigs
    _MARKET_CACHE["sector_map"] = sector_map
    _MARKET_CACHE["etfs"] = etfs
    _MARKET_CACHE["loaded"] = True
    return sigs, sector_map, etfs


def _make_strategy_callables():
    from _sweep1 import SCORE_VARIANTS, make_filter, make_score
    score_fn = make_score(SCORE_VARIANTS["pure_div"])
    filter_fn = make_filter(min_years=5, min_div_months=12)
    return score_fn, filter_fn


def _build_price_lookup(
    sigs: Dict[str, pd.DataFrame], etfs: Dict[str, Any], asof: date,
    portfolio: Portfolio,
) -> Tuple[Dict[str, float], date, List[str]]:
    """Returns (price_lookup, signal_date_used, warnings)."""
    warnings: List[str] = []
    idx = sigs["price"].index
    eligible = idx[idx <= pd.Timestamp(asof)]
    if len(eligible) == 0:
        raise ValueError(f"No signal data <= asof={asof}")
    signal_dt = eligible[-1]
    days_stale = (pd.Timestamp(asof) - signal_dt).days
    if days_stale > 35:
        warnings.append(
            f"Market signal data is {days_stale} days old (latest={signal_dt.date()}). "
            "Run `python yfinance_app.py` to refresh, then re-run advisor."
        )

    last_px = sigs["price"].loc[signal_dt]
    price_lookup: Dict[str, float] = {}
    for t in last_px.index:
        v = last_px[t]
        if pd.notna(v) and v > 0:
            price_lookup[t] = float(v)

    # ETFs (BND, SPY) must be present
    for key in ("BND", "SPY", "VTI", "SSO"):
        s = etfs[key].resample("BME").last()
        sub = s.loc[: pd.Timestamp(asof)]
        if len(sub) == 0:
            raise ValueError(f"No ETF data for {key} <= asof={asof}")
        price_lookup[key] = float(sub.iloc[-1])

    # User's actual holdings may have prices from CSV that are fresher than the pkl.
    # Use CSV price when available so the dollar-value math matches what they see in Fidelity.
    for t, pos in portfolio.holdings.items():
        if pos.price and pos.price > 0:
            price_lookup[t] = pos.price

    return price_lookup, signal_dt.date(), warnings


def _resolve_contribution(store: Store, asof: date, options: AdvisorRunOptions) -> Tuple[float, Optional[str]]:
    """Returns (dollars to flag as contribution, quarter_label).

    We DO NOT auto-inject cash; we only track whether the user should have deposited.
    The actual cash comes from the parsed CSV.
    """
    if not is_contribution_month(asof):
        return 0.0, None
    q = quarter_of(asof)
    store.upsert_contribution(q, options.contribution_quarterly, account=options.account)
    rec = store.get_contribution(q, account=options.account)
    if rec and not rec.get("deposited"):
        return options.contribution_quarterly, q
    return 0.0, q  # already deposited (informational only)


# ---- Main entry point ----

def run_advisor(
    portfolio: Portfolio,
    options: Optional[AdvisorRunOptions] = None,
    store: Optional[Store] = None,
    progress_cb: Optional[Any] = None,
) -> AdvisorRunResult:
    """Execute one advisor run end-to-end.

    `progress_cb` (optional): callable(step_idx, step_total, step_name, message)
    -- best-effort progress updates. All calls are wrapped defensively.

    Caller responsibility:
      - The portfolio.cash field should already reflect any deposited contribution.
        We TRACK that a contribution is due via the contributions ledger but do NOT
        inject phantom cash.
    """
    options = options or AdvisorRunOptions()
    store = store or Store()

    TOTAL_STEPS = 5

    def _emit(idx: int, name: str, msg: str) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(idx, TOTAL_STEPS, name, msg)
        except Exception:
            pass

    # --- cadence push-back ---
    pushback = None
    last = store.last_run(account=options.account)
    if last and not options.force_ignore_cadence:
        try:
            last_dt = datetime.fromisoformat(last["run_ts"]).date()
            days_since = (date.today() - last_dt).days
            if days_since < options.min_days_between_runs:
                pushback = (
                    f"Last run was {days_since} days ago ({last_dt}). "
                    f"Champion expects ~monthly cadence. Use force=true to override."
                )
                # Don't raise — caller decides; but we still won't execute
                return AdvisorRunResult(
                    eval=None, ai_review=None, ai_status="skipped",
                    ai_error=None, run_id=-1, contribution_due=0.0,
                    contribution_quarter=None, pushback_reason=pushback,
                )
        except (KeyError, ValueError):
            pass

    asof = portfolio.asof_date

    # --- load market ---
    _emit(1, "Loading market data", "Reading signals, sector map, ETF series...")
    sigs, sector_map, etfs = _load_market_data()
    score_fn, filter_fn = _make_strategy_callables()

    _emit(2, "Building price lookup", "Snapshotting BND/SPY + holding prices for asof")
    price_lookup, signal_date_used, warnings = _build_price_lookup(sigs, etfs, asof, portfolio)

    # --- contribution check ---
    contrib_due, contrib_q = _resolve_contribution(store, asof, options)

    # --- last_peak_spy from state (initialize on first run) ---
    last_peak_spy = store.get_last_peak_spy()
    if last_peak_spy is None:
        last_peak_spy = price_lookup["SPY"]
        store.set_last_peak_spy(last_peak_spy)

    # --- assemble shares dict (BND treated as ETF, not a candidate) ---
    portfolio_shares = portfolio.as_shares_dict()

    # --- evaluate ---
    _emit(3, "Evaluating portfolio", "Scoring universe, sector caps, dip/snapback/rebal logic")
    result = evaluate(
        portfolio_shares=portfolio_shares,
        cash=portfolio.cash,
        sigs=sigs,
        score_fn=score_fn,
        filter_fn=filter_fn,
        sector_map=sector_map,
        price_lookup=price_lookup,
        last_peak_spy=last_peak_spy,
        asof=asof,
        contribution_due=contrib_due,
        contribution_quarter=contrib_q,
        force_rebalance=options.force_rebalance,
    )

    if options.force_rebalance and asof.month != 2:
        result.notes.append(
            "FORCED REBALANCE: Annual rebalance logic ran outside the normal February cadence at user request."
        )

    for w in warnings:
        result.notes.append("DATA WARNING: " + w)

    # --- persist last_peak_spy if changed ---
    if abs(result.last_peak_spy - last_peak_spy) > 0.001:
        store.set_last_peak_spy(result.last_peak_spy)

    # --- AI review ---
    ai_review_out: Optional[Dict[str, Any]] = None
    ai_status = "disabled"
    ai_error: Optional[str] = None
    if options.enable_ai and os.environ.get("GITHUB_TOKEN"):
        try:
            from advisor.ai_review import review as ai_review

            def _ai_subcb(sub_idx: int, sub_total: int, msg: str) -> None:
                _emit(4, "AI review", msg)

            _emit(4, "AI review", "Starting AI advisory overlay")
            ai_review_out, ai_status = ai_review(
                result, portfolio, price_lookup, progress_cb=_ai_subcb,
            )
        except Exception as e:  # noqa: BLE001
            ai_status = "failed"
            ai_error = f"{type(e).__name__}: {e}"
            result.notes.append(f"AI review failed: {ai_error}")
    elif options.enable_ai:
        ai_status = "disabled"
        _emit(4, "AI review", "Skipped (GITHUB_TOKEN not set)")
    else:
        _emit(4, "AI review", "Skipped (disabled by options)")

    # --- persist run ---
    _emit(5, "Persisting run", "Writing run record to database")
    holdings_before = {t: round(sh, 6) for t, sh in result.holdings_before.items()}
    holdings_target = {t: round(sh, 6) for t, sh in result.holdings_target.items()}
    # Snapshot prices only for tickers we care about (held + target + BND/SPY)
    relevant = set(holdings_before) | set(holdings_target) | {"BND", "SPY"}
    price_snapshot = {t: round(float(price_lookup[t]), 4) for t in relevant if t in price_lookup}
    cb_total = sum(p.cost_basis_total for p in portfolio.holdings.values() if p.cost_basis_total)
    cost_basis_val = round(cb_total, 2) if cb_total > 0 else None
    cost_basis_by_ticker = {
        t: round(float(p.cost_basis_total), 2)
        for t, p in portfolio.holdings.items() if p.cost_basis_total
    }
    rec = RunRecord(
        id=None,
        run_ts=datetime.utcnow().isoformat(),
        asof_date=str(asof),
        run_type=result.run_type,
        portfolio_value=round(result.portfolio_value, 2),
        cash=round(result.cash_after, 2),
        stocks_pct=round(result.stocks_pct, 4),
        bnd_pct=round(result.bnd_pct, 4),
        last_peak_spy=round(result.last_peak_spy, 4),
        current_spy=round(result.current_spy, 4),
        spy_drawdown=round(result.spy_drawdown, 4),
        orders=[o.to_dict() for o in result.orders],
        holdings_before=holdings_before,
        holdings_target=holdings_target,
        price_snapshot=price_snapshot,
        cost_basis=cost_basis_val,
        cost_basis_by_ticker=cost_basis_by_ticker,
        ai_status=ai_status,
        ai_review=ai_review_out,
        notes="\n".join(result.notes),
        account=options.account,
    )
    run_id = store.insert_run(rec)

    return AdvisorRunResult(
        eval=result,
        ai_review=ai_review_out,
        ai_status=ai_status,
        ai_error=ai_error,
        run_id=run_id,
        contribution_due=contrib_due,
        contribution_quarter=contrib_q,
        pushback_reason=pushback,
    )
