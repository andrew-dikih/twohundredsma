"""Fidelity Portfolio_Positions CSV parser.

The Fidelity download has a known schema:
    Account Number,Account Name,Symbol,Description,Quantity,Last Price,
    Last Price Change,Current Value,Today's Gain/Loss Dollar,...,
    Cost Basis Total,Average Cost Basis,Type

After the data rows comes a blank line then disclaimer text wrapped in quotes,
followed by a "Date downloaded ..." line we use to determine asof_date.

Money-market sweep symbols (treated as CASH, not equity):
    SPAXX, FZFXX, FDRXX, FDLXX, FZDXX, FCASH
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CASH_SYMBOLS = {"SPAXX", "FZFXX", "FDRXX", "FDLXX", "FZDXX", "FCASH", "FDIC"}

_REQUIRED_COLS = {"symbol", "quantity", "current value"}


@dataclass
class Position:
    ticker: str
    shares: float
    price: float
    value: float
    cost_basis_total: Optional[float] = None
    avg_cost: Optional[float] = None
    description: str = ""
    account: str = ""


@dataclass
class Portfolio:
    asof_date: date
    holdings: Dict[str, Position] = field(default_factory=dict)
    cash: float = 0.0
    cash_positions: Dict[str, float] = field(default_factory=dict)
    accounts: List[str] = field(default_factory=list)
    raw_warnings: List[str] = field(default_factory=list)

    @property
    def total_value(self) -> float:
        return self.cash + sum(p.value for p in self.holdings.values())

    @property
    def tickers(self) -> List[str]:
        return sorted(self.holdings.keys())

    def as_shares_dict(self) -> Dict[str, float]:
        return {t: p.shares for t, p in self.holdings.items()}

    def summary(self) -> str:
        tv = self.total_value
        parts = [
            f"asof={self.asof_date}",
            f"total=${tv:,.2f}",
            f"cash=${self.cash:,.2f}" + (f" ({100*self.cash/tv:.1f}%)" if tv else ""),
            f"holdings={len(self.holdings)}",
            f"accounts={','.join(self.accounts) or 'unknown'}",
        ]
        return " | ".join(parts)

    def account_summary(self) -> str:
        return f"Accounts: {', '.join(self.accounts) or 'unknown'}"


# ---------- helpers ----------

_DATE_RE = re.compile(r"Date downloaded\s+([A-Za-z]+-\d{1,2}-\d{4})", re.IGNORECASE)


def _parse_money(s: str) -> Optional[float]:
    if s is None:
        return None
    s = s.strip().replace('"', '')
    if s == "" or s.lower() in {"n/a", "--", "-"}:
        return None
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    s = s.lstrip("+").lstrip("$").replace(",", "").rstrip("%")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _parse_qty(s: str) -> Optional[float]:
    if s is None:
        return None
    s = s.strip().replace(",", "").replace('"', '')
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_ticker(sym: str) -> str:
    return sym.strip().rstrip("*").strip().upper()


def _find_asof_date(text: str, fallback: Optional[date] = None) -> date:
    m = _DATE_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%b-%d-%Y").date()
        except ValueError:
            pass
    return fallback or date.today()


def _detect_header(rows: List[List[str]]) -> Tuple[int, Dict[str, int]]:
    for i, row in enumerate(rows[:20]):
        low = [c.strip().lower() for c in row]
        if _REQUIRED_COLS.issubset(set(low)):
            return i, {c.strip().lower(): idx for idx, c in enumerate(row)}
    raise ValueError("Could not find header row with Symbol/Quantity/Current Value")


# ---------- public API ----------

def parse_fidelity_csv(text: str, default_asof: Optional[date] = None) -> Portfolio:
    asof = _find_asof_date(text, default_asof)

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]

    header_idx, cols = _detect_header(rows)

    def col(name: str) -> Optional[int]:
        return cols.get(name.lower())

    sym_i = col("symbol")
    qty_i = col("quantity")
    val_i = col("current value")
    px_i = col("last price")
    desc_i = col("description")
    acct_i = col("account name") or col("account number")
    cb_total_i = col("cost basis total") or col("cost basis")
    cb_avg_i = col("average cost basis")

    if sym_i is None or qty_i is None or val_i is None:
        raise ValueError("Missing required columns")

    holdings: Dict[str, Position] = {}
    cash_positions: Dict[str, float] = {}
    accounts_seen: List[str] = []
    warnings: List[str] = []

    for row in rows[header_idx + 1:]:
        if len(row) < max(sym_i, val_i) + 1:
            continue
        raw_sym = row[sym_i].strip()
        if not raw_sym:
            continue
        # Skip footer disclaimer text rows (very long or contain spaces)
        if " " in raw_sym or len(raw_sym) > 20:
            continue

        ticker = _normalize_ticker(raw_sym)
        if not ticker:
            continue

        value = _parse_money(row[val_i] if val_i < len(row) else "") or 0.0

        if acct_i is not None and acct_i < len(row):
            acct = row[acct_i].strip()
            if acct and acct not in accounts_seen:
                accounts_seen.append(acct)

        if ticker in CASH_SYMBOLS:
            cash_positions[ticker] = cash_positions.get(ticker, 0.0) + value
            continue

        if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", ticker):
            warnings.append(f"Skipped non-ticker row: {ticker!r}")
            continue

        shares = _parse_qty(row[qty_i] if qty_i < len(row) else "") or 0.0
        if shares <= 0:
            warnings.append(f"Skipped {ticker}: shares={shares}")
            continue

        price = _parse_money(row[px_i] if (px_i is not None and px_i < len(row)) else "")
        if not price:
            price = (value / shares) if shares else 0.0
        desc = row[desc_i].strip() if (desc_i is not None and desc_i < len(row)) else ""
        cb_tot = _parse_money(row[cb_total_i] if (cb_total_i is not None and cb_total_i < len(row)) else "")
        cb_avg = _parse_money(row[cb_avg_i] if (cb_avg_i is not None and cb_avg_i < len(row)) else "")

        if ticker in holdings:
            ex = holdings[ticker]
            new_shares = ex.shares + shares
            new_value = ex.value + value
            new_cb = (ex.cost_basis_total or 0) + (cb_tot or 0) if (ex.cost_basis_total or cb_tot) else None
            this_acct = row[acct_i].strip() if (acct_i is not None and acct_i < len(row)) else ""
            holdings[ticker] = Position(
                ticker=ticker, shares=new_shares, price=price, value=new_value,
                cost_basis_total=new_cb,
                avg_cost=(new_cb / new_shares) if (new_cb and new_shares) else None,
                description=ex.description or desc,
                account="multi" if ex.account != this_acct else ex.account,
            )
        else:
            holdings[ticker] = Position(
                ticker=ticker, shares=shares, price=price, value=value,
                cost_basis_total=cb_tot, avg_cost=cb_avg, description=desc,
                account=row[acct_i].strip() if (acct_i is not None and acct_i < len(row)) else "",
            )

    return Portfolio(
        asof_date=asof,
        holdings=holdings,
        cash=sum(cash_positions.values()),
        cash_positions=cash_positions,
        accounts=accounts_seen,
        raw_warnings=warnings,
    )


def parse_fidelity_file(path: str | Path, default_asof: Optional[date] = None) -> Portfolio:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_fidelity_csv(text, default_asof=default_asof)
