"""Advisor engine: deterministic single-point-in-time evaluation.

Mirrors _harness.run_strategy's per-iteration logic, applied to today's portfolio
state. Produces an Orders list describing trades to execute.

Hard guarantees:
  * 100% deterministic. No AI / LLM / randomness.
  * Reproducible: same inputs (CSV + same market snapshot) -> bit-identical orders.
  * Champion strategy: pure_div, annual rebal in FEBRUARY (sweep-validated),
    monthly dip/snapback/contribution checks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Champion strategy parameters (verified)
CHAMPION_PARAMS: Dict[str, Any] = dict(
    n_holdings=20,
    stock_w_base=0.85,
    sector_cap=0.20,
    sector_cap_mode="dollar",
    min_sectors=7,
    hold_bias=0.5,
    dip_tier=[(-0.05, 1/3), (-0.10, 1/2), (-0.20, 1.0)],
    snapback=0.95,
    equal_weight=True,
    bnd_floor=0.05,
    rebal_month=2,  # FEBRUARY — sweep-validated optimum (vs hardcoded Dec)
    # When dip-buy fires, immediately deploy the freed BND-cash:
    #   'held_equal'      -> split evenly across currently-held stocks (default; +$2.2M / no risk increase)
    #   'hybrid_scout_eq' -> 50% even-split into held names, 50% buy top-ranked new names (best Calmar)
    #   None              -> legacy bug-behavior: cash sits until next Feb rebal (DO NOT USE)
    dip_deploy="scout_only",
    scout_n=5,  # number of new names to scout if dip_deploy='hybrid_scout_eq'
)

BND_TICKER = "BND"
SPY_TICKER = "SPY"

SCORE_NAME = "pure_div"
SCORE_FORMULA = "0.50·yield_z + 0.30·yield5y_z + 0.20·div_consistency_z"
FILTER_DESCRIPTION = "min_years=5, min_div_months=12 (of last 60)"


DIP_DEPLOY_DESCRIPTIONS: Dict[str, str] = {
    "held_equal": "When a dip fires, freed BND cash is split evenly across the stocks you currently hold (existing positions get topped up; no new tickers are bought).",
    "held_propvalue": "When a dip fires, freed BND cash is distributed across currently-held stocks in proportion to each name's current dollar value.",
    "scout_only": "When a dip fires, freed BND cash is used to BUY NEW top-ranked names not currently held (up to scout_n of them) — existing positions are left alone.",
    "hybrid_scout_eq": "When a dip fires, 50% of freed BND cash is split equally across currently-held names and 50% buys NEW top-ranked names (up to scout_n).",
    "rescore": "When a dip fires, the engine performs a full mid-cycle rebalance: it rescores the entire universe and rebuilds the target portfolio at today's prices.",
    "cash": "When a dip fires, freed BND cash sits as cash until the next contribution or annual rebalance redeploys it (legacy behavior).",
}


def _fmt_dip_tier(tiers) -> str:
    """[(-0.05, 1/3), (-0.10, 1/2)] -> '–5% / –10% triggers; sells 1/3 / 1/2 of BND'"""
    if not tiers:
        return "(none)"
    thresh = " / ".join(f"–{int(round(abs(t[0]) * 100))}%" for t in tiers)
    fracs = []
    for _, f in tiers:
        if abs(f - 1.0) < 1e-6:
            fracs.append("all")
        elif abs(f - 0.5) < 1e-6:
            fracs.append("1/2")
        elif abs(f - 1 / 3) < 0.02:
            fracs.append("1/3")
        elif abs(f - 0.25) < 1e-6:
            fracs.append("1/4")
        else:
            fracs.append(f"{f:.2f}")
    return f"SPY drawdown {thresh} triggers; sells {' / '.join(fracs)} of BND"


def strategy_info() -> Dict[str, Any]:
    """Single source of truth for strategy metadata shown in the UI.

    Returns a JSON-serializable dict so templates and the /api/strategy
    endpoint always reflect the live engine configuration.
    """
    p = CHAMPION_PARAMS
    stock_pct = int(round(p["stock_w_base"] * 100))
    snapback_pct = int(round(p["snapback"] * 100))
    sector_cap_pct = int(round(p["sector_cap"] * 100))
    bnd_floor_pct = int(round(p["bnd_floor"] * 100))
    hold_bias = p["hold_bias"] if p["hold_bias"] > 0 else 1.0
    keep_threshold = int(p["n_holdings"] / max(hold_bias, 0.01))
    max_per_sector = max(1, int(round(p["n_holdings"] * p["sector_cap"])))
    return {
        "score": SCORE_NAME,
        "score_formula": SCORE_FORMULA,
        "filter": FILTER_DESCRIPTION,
        "n_holdings": p["n_holdings"],
        "stock_w_base": p["stock_w_base"],
        "stock_w_base_pct": stock_pct,
        "bnd_floor": p["bnd_floor"],
        "bnd_floor_pct": bnd_floor_pct,
        "bnd_target_pct": 100 - stock_pct,
        "sector_cap": p["sector_cap"],
        "sector_cap_pct": sector_cap_pct,
        "sector_cap_mode": p.get("sector_cap_mode", "count"),
        "max_per_sector_count": max_per_sector,
        "min_sectors": p["min_sectors"],
        "hold_bias": p["hold_bias"],
        "keep_threshold": keep_threshold,
        "dip_tier": p["dip_tier"],
        "dip_tier_desc": _fmt_dip_tier(p["dip_tier"]),
        "dip_deploy": p["dip_deploy"],
        "dip_deploy_desc": DIP_DEPLOY_DESCRIPTIONS.get(
            p["dip_deploy"] or "cash",
            f"Unknown dip_deploy mode '{p['dip_deploy']}' — see advisor/dip_deploy.py.",
        ),
        "scout_n": p.get("scout_n"),
        "snapback": p["snapback"],
        "snapback_pct": snapback_pct,
        "rebal_month": p["rebal_month"],
        "rebal_month_name": ["", "January", "February", "March", "April", "May",
                             "June", "July", "August", "September", "October",
                             "November", "December"][p["rebal_month"]],
        "equal_weight": p["equal_weight"],
        # Headline values used in the footer / index page hero
        "label": f"{SCORE_NAME} champion ({p['dip_deploy']} dip-deploy)",
        "short_label": SCORE_NAME,
    }


@dataclass
class Order:
    action: str        # 'BUY' | 'SELL'
    ticker: str
    shares: float
    price: float
    dollar_value: float
    reason: str        # human-readable explanation
    category: str      # 'rebalance' | 'dip_buy' | 'snapback' | 'contribution' | 'sell_exit'

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action, "ticker": self.ticker,
            "shares": round(self.shares, 6), "price": round(self.price, 4),
            "dollar_value": round(self.dollar_value, 2),
            "reason": self.reason, "category": self.category,
        }


@dataclass
class EvaluationResult:
    asof: date
    run_type: str  # 'rebalance' | 'dip_buy' | 'snapback' | 'hold' | 'contribution' | 'mixed'
    orders: List[Order] = field(default_factory=list)
    holdings_before: Dict[str, float] = field(default_factory=dict)  # ticker -> shares
    holdings_target: Dict[str, float] = field(default_factory=dict)  # ticker -> shares
    target_weights: Dict[str, float] = field(default_factory=dict)   # ticker -> $ target
    candidate_scores: Dict[str, float] = field(default_factory=dict)
    candidate_sectors: Dict[str, str] = field(default_factory=dict)
    portfolio_value: float = 0.0
    cash_before: float = 0.0
    cash_after: float = 0.0
    stocks_pct: float = 0.0
    bnd_pct: float = 0.0
    last_peak_spy: float = 0.0
    current_spy: float = 0.0
    spy_drawdown: float = 0.0
    notes: List[str] = field(default_factory=list)
    contribution_quarter: Optional[str] = None
    contribution_amount: float = 0.0

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "asof": str(self.asof), "run_type": self.run_type,
            "orders": [o.to_dict() for o in self.orders],
            "portfolio_value": round(self.portfolio_value, 2),
            "stocks_pct": round(self.stocks_pct, 4),
            "bnd_pct": round(self.bnd_pct, 4),
            "spy_drawdown": round(self.spy_drawdown, 4),
            "current_spy": round(self.current_spy, 4),
            "last_peak_spy": round(self.last_peak_spy, 4),
            "notes": self.notes,
        }


# ----------- helpers -----------

def _sector_select(top_sorted_tickers: List[str], sector_map: Dict[str, str],
                   n_hold: int, per_sec_max: int, min_sectors: Optional[int]) -> List[str]:
    """Greedy sector-cap selection matching _harness lines 188-204."""
    picked: List[str] = []
    sec_counts: Dict[str, int] = {}
    for t in top_sorted_tickers:
        sec = sector_map.get(t, "Unknown")
        if sec_counts.get(sec, 0) < per_sec_max:
            picked.append(t)
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
        if len(picked) >= n_hold:
            break
    return picked[:n_hold]


def _apply_hold_bias(holdings: Dict[str, float], new_names: List[str],
                     top_sorted: List[str], n_hold: int, hold_bias: float,
                     sector_map: Optional[Dict[str, str]] = None,
                     per_sec_max: Optional[int] = None) -> List[str]:
    """Keep current holdings still in top n_hold/hold_bias, then fill with new picks.

    When sector_map + per_sec_max are supplied, BOTH the kept names and the
    new fills respect the per-sector cap. This prevents the "5 REITs slip
    through because 1 was already held" failure mode.
    """
    if hold_bias <= 0 or not holdings:
        if sector_map and per_sec_max:
            return new_names  # _sector_select already respected the cap
        return new_names
    keep_threshold = int(n_hold / max(hold_bias, 0.01))
    survivors = set(top_sorted[:keep_threshold])
    kept_candidates = [t for t in holdings if t in survivors]

    if sector_map is None or not per_sec_max:
        return list(dict.fromkeys(kept_candidates + new_names))[:n_hold]

    sec_counts: Dict[str, int] = {}
    merged: List[str] = []
    for t in kept_candidates:
        sec = sector_map.get(t, "Unknown")
        if sec_counts.get(sec, 0) < per_sec_max:
            merged.append(t)
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
    for t in new_names:
        if t in merged:
            continue
        sec = sector_map.get(t, "Unknown")
        if sec_counts.get(sec, 0) < per_sec_max:
            merged.append(t)
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
        if len(merged) >= n_hold:
            break
    return merged[:n_hold]


# ----------- main evaluate -----------

def evaluate(
    portfolio_shares: Dict[str, float],
    cash: float,
    sigs: Dict[str, pd.DataFrame],
    score_fn,
    filter_fn,
    sector_map: Dict[str, str],
    price_lookup: Dict[str, float],   # ticker -> current price (BND, SPY too)
    last_peak_spy: float,
    asof: date,
    contribution_due: float = 0.0,
    contribution_quarter: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    force_rebalance: bool = False,
) -> EvaluationResult:
    """Run advisor evaluation at a single point in time.

    Returns an EvaluationResult with orders. NEVER calls any AI/LLM.

    Args:
        portfolio_shares: ticker -> shares (includes BND if held)
        cash: current cash balance (positive number)
        sigs: monthly signal panels (output of compute_monthly_signals())
        score_fn, filter_fn: callables built by _sweep1.make_score/make_filter
        sector_map: ticker -> sector
        price_lookup: ticker -> latest price (must include BND, SPY,
                      and every holding + every candidate)
        last_peak_spy: highest SPY close seen since last dip-trigger reset
        asof: portfolio date
        contribution_due: $ amount to inject as cash (already counted in `cash`
                          if user already deposited; pass 0 in that case)
        contribution_quarter: 'YYYY-QN' label for the contribution (informational)
        params: override CHAMPION_PARAMS
    """
    p = dict(CHAMPION_PARAMS)
    if params:
        p.update(params)

    holdings = dict(portfolio_shares)  # don't mutate caller's
    cash_before = cash
    notes: List[str] = []
    orders: List[Order] = []

    # --- 1) Mark to market ---
    bnd_px = price_lookup.get(BND_TICKER, 0.0)
    spy_px = price_lookup.get(SPY_TICKER, 0.0)
    if spy_px <= 0:
        raise ValueError("price_lookup must include SPY")

    sh_bnd = holdings.pop(BND_TICKER, 0.0)
    v_bnd = sh_bnd * bnd_px

    def _stock_value() -> float:
        v = 0.0
        for t, sh in holdings.items():
            px = price_lookup.get(t)
            if px and px > 0:
                v += sh * px
        return v

    v_stocks = _stock_value()
    total = v_stocks + v_bnd + cash
    if total <= 0:
        raise ValueError(f"Portfolio total value is non-positive: {total}")

    # --- 2) SPY drawdown / peak update ---
    new_peak = max(last_peak_spy, spy_px)
    drawdown = (spy_px / new_peak) - 1.0 if new_peak > 0 else 0.0

    # --- 3) Decide what fires this run ---
    is_rebal_month = asof.month == p["rebal_month"] or force_rebalance
    has_contribution = contribution_due > 0

    # Dip-buy: find deepest tier crossed
    move_frac = 0.0
    for thresh, frac in sorted(p["dip_tier"]):  # most negative first
        if drawdown <= thresh:
            move_frac = max(move_frac, frac)
    dip_fires = move_frac > 0 and v_bnd > 0

    # Snapback decision is computed AFTER dip-buy moves cash around
    fired: List[str] = []

    # --- 4) Apply dip-buy (BND -> cash, to be deployed) ---
    if dip_fires:
        min_bnd = total * p["bnd_floor"]
        move_dollars = min(v_bnd * move_frac, max(0.0, v_bnd - min_bnd))
        if move_dollars > 0 and bnd_px > 0:
            sh_to_sell = move_dollars / bnd_px
            sh_bnd -= sh_to_sell
            v_bnd -= move_dollars
            cash += move_dollars
            orders.append(Order(
                action="SELL", ticker=BND_TICKER, shares=sh_to_sell, price=bnd_px,
                dollar_value=move_dollars,
                reason=f"Dip-buy: SPY drawdown {drawdown*100:.1f}% from peak (${new_peak:.2f}) triggers BND -> cash redeployment",
                category="dip_buy",
            ))
            notes.append(f"Dip-buy fired: SPY DD={drawdown*100:.1f}%, sold ${move_dollars:,.0f} of BND")
            fired.append("dip_buy")
            # Reset peak so we don't keep retriggering this tier
            new_peak = spy_px

            # --- 4b) DEPLOY freed cash into stocks (held_equal or hybrid_scout_eq) ---
            # This fixes the historical bug where cash was left sitting until Feb
            # rebal. Sweep-validated: held_equal adds +$2.2M vs no-deploy on the
            # 18y pure_div champion at zero risk increase.
            deploy_mode = p.get("dip_deploy")
            if deploy_mode and move_dollars > 0:
                from advisor.dip_deploy import compute_dip_deploy_buys
                dip_buys = compute_dip_deploy_buys(
                    deploy_mode=deploy_mode,
                    deploy_amt=move_dollars,
                    held_shares=dict(holdings),
                    price_lookup=price_lookup,
                    asof=pd.Timestamp(asof),
                    sigs=sigs,
                    score_fn=score_fn,
                    filter_fn=filter_fn,
                    sector_map=sector_map,
                    params=p,
                )
                kind_counts = {"held": 0, "scout": 0}
                kind_dollars = {"held": 0.0, "scout": 0.0}
                scout_names: List[str] = []
                for tkr, dol, kind in dip_buys:
                    px = price_lookup.get(tkr, 0.0)
                    if px <= 0 or dol <= 0:
                        continue
                    sh = dol / px
                    holdings[tkr] = holdings.get(tkr, 0.0) + sh
                    cash -= dol
                    kind_counts[kind] += 1
                    kind_dollars[kind] += dol
                    if kind == "scout":
                        scout_names.append(tkr)
                    if kind == "held":
                        reason = f"Dip-buy redeployment: equal-split of freed BND cash across held names ({deploy_mode})"
                    else:
                        reason = f"Dip-buy scout: top-ranked new {sector_map.get(tkr, '?')} name bought with freed BND cash"
                    orders.append(Order(
                        action="BUY", ticker=tkr, shares=sh, price=px,
                        dollar_value=dol, reason=reason, category="dip_buy",
                    ))
                if kind_counts["held"]:
                    notes.append(f"Dip-deploy ({deploy_mode}): ${kind_dollars['held']:,.0f} into {kind_counts['held']} held names")
                if kind_counts["scout"]:
                    notes.append(f"Dip-scout ({deploy_mode}): ${kind_dollars['scout']:,.0f} into {kind_counts['scout']} new names ({', '.join(scout_names)})")
                if not dip_buys and deploy_mode != "rescore":
                    notes.append(f"Dip-deploy ({deploy_mode}): no buys produced (no eligible names or scout returned empty)")

    # --- 5) Snapback: trim stocks if drifted too high ---
    v_stocks = _stock_value()
    total = v_stocks + v_bnd + cash
    stock_frac = v_stocks / total if total > 0 else 0.0
    if stock_frac > p["snapback"] and v_stocks > 0:
        target_stock = total * p["stock_w_base"]
        scale = target_stock / v_stocks
        for t in list(holdings.keys()):
            old_sh = holdings[t]
            new_sh = old_sh * scale
            delta_sh = old_sh - new_sh  # SELL this many
            if delta_sh > 0:
                px = price_lookup.get(t, 0.0)
                if px > 0:
                    orders.append(Order(
                        action="SELL", ticker=t, shares=delta_sh, price=px,
                        dollar_value=delta_sh * px,
                        reason=f"Snapback: stocks at {stock_frac*100:.1f}% of portfolio (>{p['snapback']*100:.0f}%); trimming to {p['stock_w_base']*100:.0f}%",
                        category="snapback",
                    ))
            holdings[t] = new_sh
        cash += (v_stocks - target_stock)
        v_stocks = target_stock
        # Add to BND if needed
        target_bnd = total * (1 - p["stock_w_base"])
        delta_bnd = target_bnd - v_bnd
        if delta_bnd > 1.0 and bnd_px > 0:
            sh_to_buy = delta_bnd / bnd_px
            sh_bnd += sh_to_buy
            cash -= delta_bnd
            v_bnd += delta_bnd
            orders.append(Order(
                action="BUY", ticker=BND_TICKER, shares=sh_to_buy, price=bnd_px,
                dollar_value=delta_bnd,
                reason="Snapback: bring BND back to target weight",
                category="snapback",
            ))
        notes.append(f"Snapback fired: stocks were {stock_frac*100:.1f}%, trimmed to target")
        fired.append("snapback")

    # --- 6) Contribution (just inject as cash if user already deposited) ---
    # (cash arg already includes deposit per app contract; we just label it)
    if has_contribution:
        notes.append(f"Contribution: ${contribution_due:,.0f} deposited for {contribution_quarter}")
        fired.append("contribution")

    # --- 7) Full rebalance (annual only, fires in Feb) ---
    candidate_scores_out: Dict[str, float] = {}
    candidate_sectors_out: Dict[str, str] = {t: sector_map.get(t, "Unknown") for t in holdings}
    target_weights_dollars: Dict[str, float] = {}
    holdings_target_shares: Dict[str, float] = {}

    if is_rebal_month:
        # Build score series at the latest signal date <= asof
        idx = sigs["price"].index
        eligible_dates = idx[idx <= pd.Timestamp(asof)]
        if len(eligible_dates) == 0:
            raise ValueError(f"No signal date <= asof={asof}")
        signal_dt = eligible_dates[-1]

        scores = score_fn(signal_dt, sigs, p)
        if filter_fn is not None:
            mask = filter_fn(signal_dt, sigs, p)
            scores = scores.where(mask, np.nan)
        scores = scores.dropna()
        if len(scores) < p["n_holdings"]:
            notes.append(f"REBAL SKIPPED: only {len(scores)} eligible candidates (need {p['n_holdings']})")
        else:
            top_sorted = scores.sort_values(ascending=False, kind="stable")
            per_sec_max = max(1, int(math.ceil(p["n_holdings"] * (p["sector_cap"] or 1.0))))
            new_names = _sector_select(
                top_sorted.index.tolist(), sector_map,
                n_hold=p["n_holdings"], per_sec_max=per_sec_max,
                min_sectors=p.get("min_sectors"),
            )
            final_names = _apply_hold_bias(
                holdings, new_names, top_sorted.index.tolist(),
                n_hold=p["n_holdings"], hold_bias=p["hold_bias"],
                sector_map=sector_map, per_sec_max=per_sec_max,
            )

            # Compute target dollar weights
            total_now = _stock_value() + v_bnd + cash
            target_stock_val = total_now * p["stock_w_base"]
            target_bnd_val = total_now * (1 - p["stock_w_base"])

            if p["equal_weight"]:
                per_name = target_stock_val / len(final_names)
                target_weights_dollars = {t: per_name for t in final_names}
            else:
                s_sub = top_sorted.reindex(final_names).clip(lower=0)
                tot_s = float(s_sub.sum())
                if tot_s == 0:
                    per_name = target_stock_val / len(final_names)
                    target_weights_dollars = {t: per_name for t in final_names}
                else:
                    target_weights_dollars = {t: target_stock_val * (s_sub[t] / tot_s) for t in final_names}

            # DOLLAR-WEIGHTED SECTOR CAP (mirror of _harness.py): if any
            # sector's total target $ exceeds sector_cap * total portfolio,
            # scale it down and redirect slack to BND. With equal_weight=True
            # and the count cap already applied at selection this is usually
            # a no-op (4 names * 4.25% = 17% < 20% cap), but it provides a
            # safety net when (a) score-weighted picks make individual names
            # larger, or (b) the sector_map disagrees with how a held name
            # ended up classified (NaN normalization, yfinance vs fundamentals).
            sector_cap_mode = p.get("sector_cap_mode", "count")
            if sector_cap_mode == "dollar" and p.get("sector_cap") and target_weights_dollars:
                cap_dollars = p["sector_cap"] * total_now
                sec_totals: Dict[str, float] = {}
                for t in target_weights_dollars:
                    sec = sector_map.get(t, "Unknown")
                    sec_totals[sec] = sec_totals.get(sec, 0.0) + target_weights_dollars[t]
                slack = 0.0
                for sec, sec_tot in sec_totals.items():
                    if sec_tot > cap_dollars + 1e-6:
                        scale = cap_dollars / sec_tot
                        for t in list(target_weights_dollars):
                            if sector_map.get(t, "Unknown") == sec:
                                reduction = target_weights_dollars[t] * (1 - scale)
                                target_weights_dollars[t] *= scale
                                slack += reduction
                target_bnd_val += slack
                if slack > 0:
                    notes.append(
                        f"Dollar sector cap clipped ${slack:,.0f} of stock targets "
                        f"into BND to keep all sectors <= {p['sector_cap']*100:.0f}%."
                    )

            # Score/sector outputs for UI
            for t in final_names:
                candidate_scores_out[t] = float(top_sorted.get(t, float("nan")))
                candidate_sectors_out[t] = sector_map.get(t, "Unknown")
            # Also record sector for currently-held tickers so the UI sector
            # breakdown can use the engine's authoritative classification.
            for t in holdings:
                if t not in candidate_sectors_out:
                    candidate_sectors_out[t] = sector_map.get(t, "Unknown")

            # Sell anything not in target
            for t in list(holdings.keys()):
                if t not in target_weights_dollars:
                    sh = holdings[t]
                    px = price_lookup.get(t, 0.0)
                    if sh > 0 and px > 0:
                        orders.append(Order(
                            action="SELL", ticker=t, shares=sh, price=px,
                            dollar_value=sh * px,
                            reason="Annual rebalance: dropped from top-20 candidate list",
                            category="sell_exit",
                        ))
                        cash += sh * px
                        holdings[t] = 0.0

            # Buy/adjust existing & new
            for t, tgt_val in target_weights_dollars.items():
                px = price_lookup.get(t)
                if not px or px <= 0:
                    notes.append(f"SKIP {t}: no live price")
                    continue
                cur_sh = holdings.get(t, 0.0)
                cur_val = cur_sh * px
                delta = tgt_val - cur_val
                # Match harness threshold: skip if |delta| < 1% of target
                if abs(delta) < 0.01 * tgt_val:
                    holdings_target_shares[t] = cur_sh
                    continue
                delta_sh = delta / px
                if delta_sh > 0:
                    orders.append(Order(
                        action="BUY", ticker=t, shares=delta_sh, price=px,
                        dollar_value=delta,
                        reason=("Annual rebalance: new top-20 pick" if cur_sh == 0
                                else f"Annual rebalance: top up to target weight"),
                        category="rebalance",
                    ))
                else:
                    orders.append(Order(
                        action="SELL", ticker=t, shares=-delta_sh, price=px,
                        dollar_value=-delta,
                        reason="Annual rebalance: trim back to equal weight",
                        category="rebalance",
                    ))
                holdings[t] = cur_sh + delta_sh
                cash -= delta
                holdings_target_shares[t] = holdings[t]

            # Adjust BND
            cur_bnd_val = sh_bnd * bnd_px
            delta_b = target_bnd_val - cur_bnd_val
            if abs(delta_b) > 1.0 and bnd_px > 0:
                sh_b = delta_b / bnd_px
                if sh_b > 0:
                    orders.append(Order(
                        action="BUY", ticker=BND_TICKER, shares=sh_b, price=bnd_px,
                        dollar_value=delta_b,
                        reason="Annual rebalance: bring BND to target weight",
                        category="rebalance",
                    ))
                else:
                    orders.append(Order(
                        action="SELL", ticker=BND_TICKER, shares=-sh_b, price=bnd_px,
                        dollar_value=-delta_b,
                        reason="Annual rebalance: trim BND to target weight",
                        category="rebalance",
                    ))
                sh_bnd += sh_b
                cash -= delta_b
            fired.append("rebalance")

    # --- 8) Determine run type label ---
    if not fired:
        run_type = "hold"
        notes.append("No triggers fired this month — hold all positions.")
    elif len(fired) == 1:
        run_type = fired[0]
    else:
        run_type = "mixed (" + "+".join(fired) + ")"

    # --- 9) Final state ---
    v_stocks_final = _stock_value()
    v_bnd_final = sh_bnd * bnd_px
    total_final = v_stocks_final + v_bnd_final + cash
    holdings_target_shares.update({t: sh for t, sh in holdings.items() if sh > 0})
    if sh_bnd > 0:
        holdings_target_shares[BND_TICKER] = sh_bnd

    return EvaluationResult(
        asof=asof,
        run_type=run_type,
        orders=orders,
        holdings_before=dict(portfolio_shares),
        holdings_target=holdings_target_shares,
        target_weights=target_weights_dollars,
        candidate_scores=candidate_scores_out,
        candidate_sectors=candidate_sectors_out,
        portfolio_value=total_final,
        cash_before=cash_before,
        cash_after=cash,
        stocks_pct=(v_stocks_final / total_final) if total_final > 0 else 0,
        bnd_pct=(v_bnd_final / total_final) if total_final > 0 else 0,
        last_peak_spy=new_peak,
        current_spy=spy_px,
        spy_drawdown=drawdown,
        notes=notes,
        contribution_quarter=contribution_quarter,
        contribution_amount=contribution_due,
    )
