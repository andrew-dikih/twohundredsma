"""Pull and analyze 3-year company financials deterministically.

Returns structured facts (computed YoY changes, ratios, auto-flagged concerns)
so the AI overlay can reason about quality without having to parse raw tables.

ETFs and tickers without filings return None gracefully.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def _safe_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _first_row(df, candidates: List[str]) -> Optional[Any]:
    """Return the first matching row (Series) for any candidate label, else None."""
    if df is None or getattr(df, "empty", True):
        return None
    idx = set(df.index.tolist())
    for name in candidates:
        if name in idx:
            return df.loc[name]
    return None


def _row_to_yearly(row, year_cols: List[Any]) -> List[Optional[float]]:
    if row is None:
        return [None] * len(year_cols)
    out: List[Optional[float]] = []
    for c in year_cols:
        try:
            out.append(_safe_float(row[c]))
        except (KeyError, IndexError):
            out.append(None)
    return out


def _yoy_pct(series: List[Optional[float]]) -> List[Optional[float]]:
    """Per-period YoY percent change. First entry is None.

    `series` is oldest -> newest.
    """
    out: List[Optional[float]] = [None]
    for i in range(1, len(series)):
        prev, cur = series[i - 1], series[i]
        if prev is None or cur is None or prev == 0:
            out.append(None)
            continue
        out.append((cur - prev) / abs(prev) * 100.0)
    return out


def _total_change_pct(series: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in series if v is not None]
    if len(vals) < 2 or vals[0] == 0:
        return None
    return (vals[-1] - vals[0]) / abs(vals[0]) * 100.0


def _trend_label(series: List[Optional[float]]) -> str:
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return "insufficient_data"
    total = _total_change_pct(series)
    if total is None:
        return "insufficient_data"
    if total > 15:
        return "growing"
    if total > 3:
        return "modest_growth"
    if total > -3:
        return "flat"
    if total > -15:
        return "declining"
    return "sharp_decline"


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    abs_v = abs(v)
    if abs_v >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs_v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}%"


def fetch_financials(ticker: str) -> Optional[Dict[str, Any]]:
    """Pull last 3 annual periods, compute structured trends.

    Returns dict with: ticker, years, metrics, ratios, summary_bullets, concerns,
    source_url. Returns None for ETFs / tickers without filings.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None

    try:
        t = yf.Ticker(ticker)
        inc = t.income_stmt  # annual income statement (newest col first)
        bs = t.balance_sheet
        cf = t.cashflow
    except Exception:
        return None

    if inc is None or getattr(inc, "empty", True):
        return None

    # Take up to 3 most recent annual periods, then reorder oldest -> newest
    cols = list(inc.columns)[:3]
    if len(cols) < 2:
        return None
    cols = list(reversed(cols))
    year_labels = [str(c.year) if hasattr(c, "year") else str(c)[:4] for c in cols]

    revenue = _row_to_yearly(_first_row(inc, ["Total Revenue", "Revenue"]), cols)
    net_income = _row_to_yearly(_first_row(inc, ["Net Income", "Net Income Common Stockholders"]), cols)
    op_income = _row_to_yearly(_first_row(inc, ["Operating Income", "Operating Revenue"]), cols)
    diluted_eps = _row_to_yearly(_first_row(inc, ["Diluted EPS", "Basic EPS"]), cols)
    interest_exp = _row_to_yearly(_first_row(inc, ["Interest Expense"]), cols)

    total_debt = _row_to_yearly(_first_row(bs, ["Total Debt", "Long Term Debt"]), cols)
    equity = _row_to_yearly(_first_row(bs, ["Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"]), cols)
    cash = _row_to_yearly(_first_row(bs, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]), cols)

    op_cf = _row_to_yearly(_first_row(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]), cols)
    capex = _row_to_yearly(_first_row(cf, ["Capital Expenditure"]), cols)
    fcf_direct = _row_to_yearly(_first_row(cf, ["Free Cash Flow"]), cols)
    divs_paid = _row_to_yearly(_first_row(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid"]), cols)

    # Compute FCF (operating cash flow - capex, capex usually negative in yfinance).
    fcf: List[Optional[float]] = []
    for i, direct in enumerate(fcf_direct):
        if direct is not None:
            fcf.append(direct)
            continue
        ocf, cx = op_cf[i], capex[i]
        if ocf is None or cx is None:
            fcf.append(None)
        else:
            fcf.append(ocf + cx if cx < 0 else ocf - cx)

    metrics = {
        "revenue": {"values": revenue, "yoy_pct": _yoy_pct(revenue),
                    "total_change_pct": _total_change_pct(revenue),
                    "trend": _trend_label(revenue)},
        "net_income": {"values": net_income, "yoy_pct": _yoy_pct(net_income),
                       "total_change_pct": _total_change_pct(net_income),
                       "trend": _trend_label(net_income)},
        "operating_income": {"values": op_income, "yoy_pct": _yoy_pct(op_income),
                             "total_change_pct": _total_change_pct(op_income),
                             "trend": _trend_label(op_income)},
        "diluted_eps": {"values": diluted_eps, "yoy_pct": _yoy_pct(diluted_eps),
                        "total_change_pct": _total_change_pct(diluted_eps),
                        "trend": _trend_label(diluted_eps)},
        "free_cash_flow": {"values": fcf, "yoy_pct": _yoy_pct(fcf),
                           "total_change_pct": _total_change_pct(fcf),
                           "trend": _trend_label(fcf)},
        "total_debt": {"values": total_debt, "yoy_pct": _yoy_pct(total_debt),
                       "total_change_pct": _total_change_pct(total_debt),
                       "trend": _trend_label(total_debt)},
    }

    # --- Latest-period ratios ---
    ratios: Dict[str, Optional[float]] = {}
    if revenue[-1] and net_income[-1] is not None:
        ratios["net_margin_pct"] = (net_income[-1] / revenue[-1]) * 100.0
    if revenue[-1] and fcf[-1] is not None:
        ratios["fcf_margin_pct"] = (fcf[-1] / revenue[-1]) * 100.0
    if total_debt[-1] is not None and equity[-1] not in (None, 0):
        ratios["debt_to_equity"] = total_debt[-1] / equity[-1]
    if divs_paid[-1] is not None and net_income[-1] not in (None, 0):
        # divs_paid is typically negative in yfinance (outflow)
        ratios["payout_ratio_pct"] = abs(divs_paid[-1]) / abs(net_income[-1]) * 100.0
    if interest_exp[-1] not in (None, 0) and op_income[-1] is not None:
        # Interest expense is positive in yfinance
        try:
            ratios["interest_coverage"] = op_income[-1] / abs(interest_exp[-1])
        except ZeroDivisionError:
            pass

    # --- Auto-flagged concerns (deterministic, no LLM) ---
    concerns: List[str] = []
    rev_trend = metrics["revenue"]["trend"]
    ni_trend = metrics["net_income"]["trend"]
    fcf_trend = metrics["free_cash_flow"]["trend"]
    if rev_trend in ("declining", "sharp_decline"):
        concerns.append(f"Revenue {rev_trend.replace('_',' ')} over 3yr ({_fmt_pct(metrics['revenue']['total_change_pct'])} total)")
    if ni_trend in ("declining", "sharp_decline"):
        concerns.append(f"Net income {ni_trend.replace('_',' ')} over 3yr ({_fmt_pct(metrics['net_income']['total_change_pct'])} total)")
    if fcf_trend in ("declining", "sharp_decline"):
        concerns.append(f"FCF {fcf_trend.replace('_',' ')} over 3yr ({_fmt_pct(metrics['free_cash_flow']['total_change_pct'])} total)")
    if fcf[-1] is not None and fcf[-1] < 0:
        concerns.append(f"Most recent FCF is negative ({_fmt_money(fcf[-1])})")
    if ratios.get("payout_ratio_pct") is not None and ratios["payout_ratio_pct"] > 90:
        concerns.append(f"Dividend payout ratio {ratios['payout_ratio_pct']:.0f}% — limited headroom")
    if ratios.get("debt_to_equity") is not None and ratios["debt_to_equity"] > 2.0:
        concerns.append(f"Debt/equity {ratios['debt_to_equity']:.2f} — high leverage")
    if ratios.get("interest_coverage") is not None and ratios["interest_coverage"] < 2.5:
        concerns.append(f"Interest coverage {ratios['interest_coverage']:.1f}x — debt servicing risk")
    if ratios.get("net_margin_pct") is not None and ratios["net_margin_pct"] < 0:
        concerns.append(f"Negative net margin ({ratios['net_margin_pct']:.1f}%)")

    # --- Human-readable summary bullets (for prompt + UI fallback) ---
    bullets: List[str] = []
    bullets.append(
        f"Revenue {rev_trend.replace('_',' ')}: {_fmt_money(revenue[0])} ({year_labels[0]}) -> "
        f"{_fmt_money(revenue[-1])} ({year_labels[-1]}), {_fmt_pct(metrics['revenue']['total_change_pct'])} total"
    )
    bullets.append(
        f"Net income {ni_trend.replace('_',' ')}: {_fmt_money(net_income[0])} -> {_fmt_money(net_income[-1])}, "
        f"{_fmt_pct(metrics['net_income']['total_change_pct'])} total"
    )
    bullets.append(
        f"FCF {fcf_trend.replace('_',' ')}: {_fmt_money(fcf[0])} -> {_fmt_money(fcf[-1])}, "
        f"{_fmt_pct(metrics['free_cash_flow']['total_change_pct'])} total"
    )
    if ratios.get("net_margin_pct") is not None:
        bullets.append(f"Net margin (latest): {ratios['net_margin_pct']:.1f}%")
    if ratios.get("fcf_margin_pct") is not None:
        bullets.append(f"FCF margin (latest): {ratios['fcf_margin_pct']:.1f}%")
    if ratios.get("debt_to_equity") is not None:
        bullets.append(f"Debt/equity (latest): {ratios['debt_to_equity']:.2f}")
    if ratios.get("payout_ratio_pct") is not None:
        bullets.append(f"Dividend payout ratio (latest): {ratios['payout_ratio_pct']:.0f}%")
    if ratios.get("interest_coverage") is not None:
        bullets.append(f"Interest coverage (latest): {ratios['interest_coverage']:.1f}x")

    return {
        "ticker": ticker.upper(),
        "years": year_labels,
        "metrics": metrics,
        "ratios": ratios,
        "summary_bullets": bullets,
        "concerns": concerns,
        "source_url": f"https://finance.yahoo.com/quote/{ticker.upper()}/financials",
    }
