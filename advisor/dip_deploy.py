"""Shared dip-deploy decision logic.

Both the live monthly advisor (`advisor/engine.py`) and the multi-year backtest
harness (`_harness.py`) call `compute_dip_deploy_buys()` so they always make
identical buying decisions when a SPY drawdown frees up BND cash. Each caller
adapts the returned (ticker, dollar_amount, kind) list to its own state model:

- The live engine wraps each entry in an `Order` for the UI.
- The harness mutates its `holdings` dict + cash counter.

If you add a new dip_deploy mode, add it here ONCE — both call sites pick it
up automatically.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# (ticker, dollar_amount, kind) where kind is 'held' or 'scout'
DipBuy = Tuple[str, float, str]


def _held_equal_buys(
    deploy_amt: float,
    held_tickers: List[str],
    price_lookup: Dict[str, float],
) -> List[DipBuy]:
    eligible = [t for t in held_tickers if price_lookup.get(t, 0) > 0]
    if not eligible or deploy_amt <= 0:
        return []
    per_name = deploy_amt / len(eligible)
    return [(t, per_name, "held") for t in eligible]


def _held_propvalue_buys(
    deploy_amt: float,
    held_shares: Dict[str, float],
    price_lookup: Dict[str, float],
) -> List[DipBuy]:
    """Distribute `deploy_amt` proportional to each held name's current $ value."""
    values: Dict[str, float] = {}
    for t, sh in held_shares.items():
        px = price_lookup.get(t, 0.0)
        if px > 0:
            values[t] = sh * px
    total = sum(values.values())
    if total <= 0 or deploy_amt <= 0:
        return []
    return [(t, deploy_amt * (v / total), "held") for t, v in values.items()]


def _scout_picks(
    asof: pd.Timestamp,
    sigs: Dict[str, pd.DataFrame],
    score_fn,
    filter_fn,
    held_tickers: List[str],
    price_lookup: Dict[str, float],
    sector_map: Dict[str, str],
    params: Dict[str, Any],
) -> List[str]:
    """Top-ranked new names not currently held, respecting the sector cap if set."""
    sig_idx = sigs.get("price").index if "price" in sigs else None
    if sig_idx is None or len(sig_idx) == 0:
        return []
    valid = sig_idx[sig_idx <= asof]
    if len(valid) == 0:
        return []
    sig_date = valid[-1]

    scores = score_fn(sig_date, sigs, params)
    if filter_fn is not None:
        mask = filter_fn(sig_date, sigs, params)
        scores = scores.where(mask, np.nan)
    scores = scores.dropna().sort_values(ascending=False)

    held = set(held_tickers)
    cands = [t for t in scores.index if t not in held and price_lookup.get(t, 0) > 0]

    scout_n = int(params.get("scout_n", max(1, int(params.get("n_holdings", 20)) // 4)))
    sector_cap = params.get("sector_cap")
    if not sector_cap:
        return cands[:scout_n]

    sec_counts: Dict[str, int] = {}
    for h in held_tickers:
        sec = sector_map.get(h, "Unknown")
        sec_counts[sec] = sec_counts.get(sec, 0) + 1
    per_sec_max = max(1, int(np.ceil(int(params.get("n_holdings", 20)) * sector_cap)))
    picks: List[str] = []
    for c in cands:
        sec = sector_map.get(c, "Unknown")
        if sec_counts.get(sec, 0) < per_sec_max:
            picks.append(c)
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
        if len(picks) >= scout_n:
            break
    return picks


def compute_dip_deploy_buys(
    deploy_mode: Optional[str],
    deploy_amt: float,
    held_shares: Dict[str, float],
    price_lookup: Dict[str, float],
    asof: pd.Timestamp,
    sigs: Dict[str, pd.DataFrame],
    score_fn,
    filter_fn,
    sector_map: Dict[str, str],
    params: Dict[str, Any],
) -> List[DipBuy]:
    """Decide how to spend `deploy_amt` of freed cash when a SPY dip fires.

    Returns a list of (ticker, dollar_amount, kind) buys. Caller is responsible
    for actually applying them (share math, order objects, etc).

    Modes:
      None / 'cash'     -> []  (cash sits; next contribution/rebal redeploys it)
      'held_equal'      -> split evenly across currently-held stocks
      'held_propvalue'  -> split across held stocks proportional to $ value
      'scout_only'      -> buy up to `scout_n` NEW top-ranked names
      'hybrid_scout_eq' -> 50% even-split into held, 50% scout new names
    """
    if not deploy_mode or deploy_mode == "cash" or deploy_amt <= 0:
        return []

    held_tickers = list(held_shares.keys())
    buys: List[DipBuy] = []

    if deploy_mode == "held_equal":
        return _held_equal_buys(deploy_amt, held_tickers, price_lookup)

    if deploy_mode == "held_propvalue":
        return _held_propvalue_buys(deploy_amt, held_shares, price_lookup)

    if deploy_mode == "scout_only":
        picks = _scout_picks(asof, sigs, score_fn, filter_fn, held_tickers,
                             price_lookup, sector_map, params)
        if not picks:
            return []
        per_pick = deploy_amt / len(picks)
        return [(t, per_pick, "scout") for t in picks]

    if deploy_mode == "hybrid_scout_eq":
        if held_tickers:
            held_share = deploy_amt / 2.0
            buys.extend(_held_equal_buys(held_share, held_tickers, price_lookup))
            scout_budget = deploy_amt - held_share
        else:
            scout_budget = deploy_amt
        picks = _scout_picks(asof, sigs, score_fn, filter_fn, held_tickers,
                             price_lookup, sector_map, params)
        if picks and scout_budget > 0:
            per_pick = scout_budget / len(picks)
            buys.extend([(t, per_pick, "scout") for t in picks])
        return buys

    # 'rescore' is handled by the caller (it triggers a full rebal, not a
    # localized deployment). Return empty so the caller knows not to buy
    # anything here.
    return []
