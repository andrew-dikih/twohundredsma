"""FastAPI app for the portfolio advisor.

Runs on port 8001 (separate from the existing fastapi_app on 8000).

Routes:
    GET  /                  landing — last run, "Run Now", contribution banner
    GET  /upload            CSV upload form
    POST /upload            parse CSV → store in session, show preview
    POST /run               execute advisor → redirect to /runs/{id}
    GET  /runs/{id}         run detail page (orders, allocations, AI review)
    GET  /history           list past runs
    POST /contributions/{q}/deposited  mark a quarter as deposited
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from advisor.parser import Portfolio, parse_fidelity_csv
from advisor.engine import strategy_info
from advisor.service import AdvisorRunOptions, run_advisor
from advisor.state import (
    DEFAULT_ACCOUNT,
    Store,
    is_contribution_month,
    quarter_of,
)
from advisor.auth import (
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    hash_password,
    verify_password,
    new_session_token,
    is_admin_username,
    optional_user,
    require_user,
)

APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

app = FastAPI(title="Portfolio Advisor", version="0.1.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_TICKER_META_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_YIELD_CACHE_FILE: Optional[Any] = None
_YIELD_CACHE: Optional[Dict[str, Any]] = None

def _yield_cache_path():
    global _YIELD_CACHE_FILE
    if _YIELD_CACHE_FILE is None:
        from pathlib import Path as _P
        _YIELD_CACHE_FILE = _P(__file__).resolve().parent.parent / "dat" / "ticker_yield_cache.json"
    return _YIELD_CACHE_FILE

def _load_yield_cache() -> Dict[str, Any]:
    global _YIELD_CACHE
    if _YIELD_CACHE is not None:
        return _YIELD_CACHE
    import json
    p = _yield_cache_path()
    if p.exists():
        try:
            _YIELD_CACHE = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _YIELD_CACHE = {}
    else:
        _YIELD_CACHE = {}
    return _YIELD_CACHE

def _save_yield_cache() -> None:
    import json
    try:
        _yield_cache_path().write_text(json.dumps(_load_yield_cache()), encoding="utf-8")
    except Exception:
        pass

def _fetch_yf_metadata(symbol: str) -> Dict[str, Any]:
    """One-shot yfinance lookup for tickers missing from our cached snapshots.
    Returns {dy, name} and persists to dat/ticker_yield_cache.json with a 7-day TTL."""
    import time
    cache = _load_yield_cache()
    entry = cache.get(symbol)
    now = time.time()
    if entry and (now - entry.get("ts", 0) < 7 * 86400) and "name" in entry:
        return {"dy": entry.get("dy"), "name": entry.get("name") or "", "sector": entry.get("sector") or ""}
    dy = None
    name = ""
    sector = ""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).get_info()
        raw = info.get("dividendYield")
        if raw is None:
            raw = info.get("trailingAnnualDividendYield")
        if raw is not None:
            raw = float(raw)
            # yfinance returns percent directly in recent versions; only rescale
            # absurdly small values that look like genuine fractions.
            if 0 < raw < 0.005:
                raw = raw * 100
            dy = round(raw, 2)
        name = (info.get("longName") or info.get("shortName") or "").strip()
        sector = (info.get("sector") or info.get("quoteType") or "").strip()
    except Exception:
        pass
    cache[symbol] = {"dy": dy, "name": name, "sector": sector, "ts": now}
    _save_yield_cache()
    return {"dy": dy, "name": name, "sector": sector}

def _load_ticker_metadata() -> Dict[str, Dict[str, str]]:
    """Build {SYMBOL: {name, url, dividend_yield}} from cached fundamentals + results JSON.

    Sources merged in priority order (later overrides earlier where present):
      1. Hard-coded ETF/fund yields (BND, SSO, VTI, SPY, etc. — not covered elsewhere)
      2. Latest fundamentals_*.pkl (yfinance fundamentals snapshot, ~1400 tickers)
      3. Latest results_*.json (screened universe, has Company Name + Google Finance URL)
    """
    global _TICKER_META_CACHE
    if _TICKER_META_CACHE is not None:
        return _TICKER_META_CACHE
    from pathlib import Path as _P
    import json

    meta: Dict[str, Dict[str, Any]] = {}

    # 1) Hardcoded ETF/fund yields + names + sector classification
    etf_info = {
        "BND":  ("Vanguard Total Bond Market ETF", 3.85, "Bonds"),
        "AGG":  ("iShares Core U.S. Aggregate Bond ETF", 3.80, "Bonds"),
        "SSO":  ("ProShares Ultra S&P 500", 0.60, "Leveraged Equity ETF"),
        "SPY":  ("SPDR S&P 500 ETF Trust", 1.27, "Equity ETF"),
        "VOO":  ("Vanguard S&P 500 ETF", 1.27, "Equity ETF"),
        "IVV":  ("iShares Core S&P 500 ETF", 1.27, "Equity ETF"),
        "VTI":  ("Vanguard Total Stock Market ETF", 1.30, "Equity ETF"),
        "ITOT": ("iShares Core S&P Total U.S. Stock Market ETF", 1.30, "Equity ETF"),
        "QQQ":  ("Invesco QQQ Trust", 0.55, "Equity ETF"),
        "QQQM": ("Invesco NASDAQ 100 ETF", 0.56, "Equity ETF"),
        "VEA":  ("Vanguard FTSE Developed Markets ETF", 3.10, "Equity ETF"),
        "VWO":  ("Vanguard FTSE Emerging Markets ETF", 2.80, "Equity ETF"),
        "VXUS": ("Vanguard Total International Stock ETF", 3.20, "Equity ETF"),
        "SCHD": ("Schwab U.S. Dividend Equity ETF", 3.60, "Equity ETF"),
        "VYM":  ("Vanguard High Dividend Yield ETF", 2.80, "Equity ETF"),
        "HDV":  ("iShares Core High Dividend ETF", 3.50, "Equity ETF"),
    }
    for sym, (name, dy, sector) in etf_info.items():
        meta[sym] = {"name": name, "url": "", "dividend_yield": dy, "sector": sector}

    dat = _P(__file__).resolve().parent.parent / "dat"

    # 2) fundamentals pickle (broadest stock coverage)
    try:
        import pandas as pd
        fund_files = sorted(dat.glob("fundamentals_*.pkl"))
        # prefer the most recent non-progress file
        fund_files = [f for f in fund_files if "progress" not in f.name]
        if fund_files:
            df = pd.read_pickle(fund_files[-1])
            if "sector" in df.columns:
                for sym, sec in df["sector"].dropna().items():
                    s = str(sym).strip().upper()
                    entry = meta.setdefault(s, {"name": "", "url": "", "dividend_yield": None, "sector": ""})
                    if not entry.get("sector"):
                        entry["sector"] = str(sec).strip()
            if "dividendYield" in df.columns:
                col = df["dividendYield"]
                if col.dropna().median() < 1:
                    col = col * 100
                for sym, dy in col.dropna().items():
                    s = str(sym).strip().upper()
                    meta.setdefault(s, {"name": "", "url": "", "dividend_yield": None})
                    meta[s]["dividend_yield"] = round(float(dy), 2)
            # fallback for stocks where current dividendYield is NaN but 5yr avg exists
            if "fiveYearAvgDividendYield" in df.columns:
                col5 = df["fiveYearAvgDividendYield"]
                if col5.dropna().median() < 1:
                    col5 = col5 * 100
                for sym, dy in col5.dropna().items():
                    s = str(sym).strip().upper()
                    entry = meta.setdefault(s, {"name": "", "url": "", "dividend_yield": None})
                    if entry.get("dividend_yield") is None:
                        entry["dividend_yield"] = round(float(dy), 2)
    except Exception:
        pass

    # 3) results JSON (canonical Company Name + Google Finance URL + recent yield)
    try:
        result_files = sorted(dat.glob("results_*.json"))
        if result_files:
            data = json.loads(result_files[-1].read_text(encoding="utf-8"))
            for row in data:
                sym = str(row.get("Ticker", "")).strip().upper()
                if not sym:
                    continue
                entry = meta.setdefault(sym, {"name": "", "url": "", "dividend_yield": None})
                if row.get("Company Name"):
                    entry["name"] = row["Company Name"].strip()
                if row.get("url"):
                    entry["url"] = row["url"].strip()
                dy = row.get("Dividend Yield %")
                if dy is not None:
                    entry["dividend_yield"] = round(float(dy), 2)
    except Exception:
        pass

    _TICKER_META_CACHE = meta
    return meta


def _ticker_link(symbol: str) -> str:
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    meta = _load_ticker_metadata().get(s, {})
    url = meta.get("url") or f"https://www.google.com/search?q={s}+stock+price"
    return f'<a href="{url}" target="_blank" rel="noopener" class="ticker-link">{s}</a>'


def _ticker_name(symbol: str) -> str:
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    name = _load_ticker_metadata().get(s, {}).get("name", "")
    if not name:
        info = _fetch_yf_metadata(s)
        name = info.get("name") or ""
        if name:
            entry = _load_ticker_metadata().setdefault(s, {"name": "", "url": "", "dividend_yield": None})
            entry["name"] = name
    return name


def _ticker_yield(symbol: str):
    if not symbol:
        return None
    s = str(symbol).strip().upper()
    dy = _load_ticker_metadata().get(s, {}).get("dividend_yield")
    if dy is None:
        info = _fetch_yf_metadata(s)
        dy = info.get("dy")
        if dy is not None:
            entry = _load_ticker_metadata().setdefault(s, {"name": "", "url": "", "dividend_yield": None})
            entry["dividend_yield"] = dy
    return dy


def _ticker_sector(symbol: str) -> str:
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    sec = _load_ticker_metadata().get(s, {}).get("sector", "")
    if not sec:
        info = _fetch_yf_metadata(s)
        sec = info.get("sector") or ""
        if sec:
            entry = _load_ticker_metadata().setdefault(s, {"name": "", "url": "", "dividend_yield": None, "sector": ""})
            entry["sector"] = sec
    return sec or "Unknown"


templates.env.globals["ticker_link"] = _ticker_link
templates.env.globals["ticker_name"] = _ticker_name
templates.env.globals["ticker_yield"] = _ticker_yield
templates.env.globals["ticker_sector"] = _ticker_sector
templates.env.globals["strategy"] = strategy_info()
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_store_lock = threading.Lock()
_pending_portfolios: Dict[str, Portfolio] = {}  # token -> parsed portfolio (one-shot)

# Single-worker executor serializes advisor runs (matches prior _store_lock behavior).
# Queueing is automatic — additional jobs sit in `queued` status until the worker picks them up.
_advisor_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="advisor-worker")
_log = logging.getLogger("advisor.jobs")


@app.on_event("startup")
def _cleanup_stale_jobs_on_startup() -> None:
    """Mark any queued/running jobs as failed (they were orphaned by the restart)."""
    try:
        n = Store().cleanup_stale_jobs()
        if n > 0:
            _log.warning("Marked %d stale advisor job(s) as failed on startup", n)
    except Exception as e:  # noqa: BLE001
        _log.error("Stale job cleanup failed: %s", e)


def _run_advisor_job(job_id: int, account: str, portfolio: Portfolio,
                     force_ignore_cadence: bool, force_rebalance: bool,
                     user_id: Optional[int] = None, enable_ai: bool = True) -> None:
    """Worker entry point. Runs in the advisor executor thread."""
    store = Store()
    try:
        store.mark_job_running(job_id)
    except Exception as e:  # noqa: BLE001
        _log.error("Could not mark job %d running: %s", job_id, e)
        return

    def progress_cb(step_idx: int, step_total: int, step_name: str, message: str) -> None:
        try:
            store.update_job_progress(
                job_id, step_idx=step_idx, step_total=step_total,
                step_name=step_name, message=message,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("Job %d progress update failed (non-fatal): %s", job_id, e)

    try:
        with _store_lock:
            out = run_advisor(
                portfolio,
                options=AdvisorRunOptions(
                    enable_ai=enable_ai,
                    force_ignore_cadence=force_ignore_cadence,
                    force_rebalance=force_rebalance,
                    account=account,
                ),
                store=store,
                progress_cb=progress_cb,
            )
    except Exception as e:  # noqa: BLE001
        _log.exception("Advisor job %d crashed", job_id)
        store.mark_job_failed(job_id, f"{type(e).__name__}: {e}")
        return

    # Cadence pushback path: re-stash the portfolio under a fresh token so the user
    # can confirm on /cadence_warning, and record the redirect URL on the job.
    if out.pushback_reason:
        token = secrets.token_urlsafe(16)
        _pending_portfolios[token] = portfolio
        rebal_q = "1" if force_rebalance else ""
        url = f"/cadence_warning?token={token}&reason={out.pushback_reason}&force_rebalance={rebal_q}"
        store.mark_job_pushback(job_id, out.pushback_reason, url)
        return

    store.mark_job_done(job_id, run_id=out.run_id)

    # Log AI usage against the user's daily quota only on successful AI runs.
    # 'disabled', 'failed', 'skipped' don't count — the user didn't actually
    # consume their daily allotment.
    if user_id is not None and out.ai_status in ("full", "partial"):
        try:
            store.log_ai_usage(user_id, run_id=out.run_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("Failed to log AI usage for user %d run %d: %s",
                         user_id, out.run_id, e)


def get_store() -> Store:
    return Store()


def _ai_globally_enabled() -> bool:
    """True iff the AI subsystem is available at all (token set, not disabled)."""
    return bool(os.environ.get("GITHUB_TOKEN")) and os.environ.get("AI_REVIEW_DISABLE") != "1"


def _ai_enabled_for_user(user: Dict[str, Any], store: Store) -> bool:
    """Whether AI review should run for THIS user on a new run today.

    Admin: always on (subject to AI being globally enabled).
    Regular: on iff they haven't already consumed a successful AI run today.
    """
    if not _ai_globally_enabled():
        return False
    if (user.get("type") or "regular") == "admin":
        return True
    return not store.ai_used_today(int(user["id"]))


def _current_account(request: Request, user: Dict[str, Any], store: Store) -> str:
    """Return the account slug the current user is currently scoped to.

    Reads the `account` cookie; falls back to the user's first account if
    the cookie is missing or refers to an account they don't own.
    """
    val = request.cookies.get("account") or ""
    if val and store.user_owns_account_slug(int(user["id"]), val):
        return val
    accts = store.list_user_accounts(int(user["id"]))
    return accts[0]["slug"] if accts else DEFAULT_ACCOUNT


def _base_ctx(request: Request, store: Store,
              user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    today = date.today()
    user = user if user is not None else optional_user(request)
    ai_global = _ai_globally_enabled()
    pending_contrib = None
    accounts: List[tuple] = []
    current_account_id = DEFAULT_ACCOUNT
    current_account_label = DEFAULT_ACCOUNT
    ai_quota_used = False
    ai_for_user = False
    if user is not None:
        accts = store.list_user_accounts(int(user["id"]))
        accounts = [(a["slug"], a["label"]) for a in accts]
        current_account_id = _current_account(request, user, store)
        current_account_label = next(
            (lbl for sid, lbl in accounts if sid == current_account_id),
            current_account_id,
        )
        if is_contribution_month(today):
            q = quarter_of(today)
            rec = store.get_contribution(q, account=current_account_id)
            if rec is None or not rec.get("deposited"):
                pending_contrib = {"quarter": q, "amount": 1750.0}
        ai_for_user = _ai_enabled_for_user(user, store)
        if (user.get("type") or "regular") != "admin":
            ai_quota_used = ai_global and store.ai_used_today(int(user["id"]))
    return {
        "request": request,
        "user": user,
        "ai_enabled": ai_for_user,
        "ai_off_banner": not ai_global,
        "ai_quota_used": ai_quota_used,
        "pending_contribution": pending_contrib,
        "today": today.isoformat(),
        "accounts": accounts,
        "current_account_id": current_account_id,
        "current_account_label": current_account_label,
    }


# ----- Routes -----

@app.exception_handler(StarletteHTTPException)
async def _auth_redirect_handler(request: Request, exc: StarletteHTTPException):
    """Convert require_user's 307 "login_required" into an actual browser redirect
    to /login, preserving the originally requested path as ?next=... so we can
    bounce the user back after they sign in. Other HTTPExceptions pass through
    to FastAPI's default handler."""
    if exc.status_code == 307 and exc.detail == "login_required":
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        # Don't redirect API requests — return JSON 401 so XHR/fetch can handle it.
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "login_required"}, status_code=401)
        return RedirectResponse(
            url=f"/login?next={next_url}",
            status_code=303,
        )
    # Fallback to default JSON error
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---- Auth: register / login / logout ----

@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request, error: str = ""):
    if optional_user(request):
        return RedirectResponse(url="/", status_code=303)
    store = get_store()
    return templates.TemplateResponse(request, "register.html", {
        "request": request,
        "user": None,
        "error": error,
        "today": date.today().isoformat(),
        "ai_off_banner": not _ai_globally_enabled(),
        "accounts": [],
        "current_account_id": "",
        "current_account_label": "",
        "ai_enabled": False,
        "ai_quota_used": False,
        "pending_contribution": None,
    })


@app.post("/register")
def register_submit(request: Request,
                    username: str = Form(...),
                    password: str = Form(...),
                    password2: str = Form(...)):
    username = (username or "").strip()
    if not username or not password:
        return RedirectResponse(url="/register?error=Username+and+password+required",
                                status_code=303)
    if len(password) < 8:
        return RedirectResponse(url="/register?error=Password+must+be+at+least+8+characters",
                                status_code=303)
    if password != password2:
        return RedirectResponse(url="/register?error=Passwords+do+not+match",
                                status_code=303)
    store = get_store()
    if store.get_user_by_username(username) is not None:
        return RedirectResponse(url="/register?error=Username+already+taken",
                                status_code=303)
    user_type = "admin" if is_admin_username(username) else "regular"
    uid = store.create_user(
        username=username,
        password_hash=hash_password(password),
        user_type=user_type,
    )
    # New regular users start with one default account so they can immediately
    # upload a portfolio without having to navigate Accounts first.
    if user_type == "regular":
        try:
            store.create_user_account(uid, label=f"{username}'s Portfolio")
        except Exception as e:  # noqa: BLE001
            _log.warning("Failed to create default account for new user %s: %s", username, e)
    # Auto-login
    token = new_session_token()
    store.create_session(uid, token, ttl_days=SESSION_TTL_DAYS)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token,
                    max_age=SESSION_TTL_DAYS * 86400,
                    httponly=True, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = "", next: str = "/"):
    if optional_user(request):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "user": None,
        "error": error,
        "next_url": next or "/",
        "today": date.today().isoformat(),
        "ai_off_banner": not _ai_globally_enabled(),
        "accounts": [],
        "current_account_id": "",
        "current_account_label": "",
        "ai_enabled": False,
        "ai_quota_used": False,
        "pending_contribution": None,
    })


@app.post("/login")
def login_submit(request: Request,
                 username: str = Form(...),
                 password: str = Form(...),
                 next: str = Form("/")):
    store = get_store()
    user = store.get_user_by_username((username or "").strip())
    if user is None or not verify_password(password or "", user["password_hash"]):
        # Avoid leaking which field was wrong
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/login?error=Invalid+username+or+password&next={quote(next or '/')}",
            status_code=303,
        )
    token = new_session_token()
    store.create_session(int(user["id"]), token, ttl_days=SESSION_TTL_DAYS)
    resp = RedirectResponse(url=next or "/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token,
                    max_age=SESSION_TTL_DAYS * 86400,
                    httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            Store().delete_session(token)
        except Exception:  # noqa: BLE001
            pass
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---- Account management (per-user) ----

@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    ctx["all_accounts"] = store.list_user_accounts(int(user["id"]))
    return templates.TemplateResponse(request, "accounts.html", ctx)


@app.post("/accounts/new")
def accounts_create(request: Request,
                    label: str = Form(...),
                    user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    try:
        new_acct = store.create_user_account(int(user["id"]), label=label)
    except ValueError:
        return RedirectResponse(url="/accounts", status_code=303)
    # Switch to the new account immediately so the user sees their freshly created scope.
    resp = RedirectResponse(url="/accounts", status_code=303)
    resp.set_cookie("account", new_acct["slug"],
                    max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax")
    return resp


@app.post("/accounts/{account_id}/delete")
def accounts_delete(request: Request, account_id: int,
                    user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    remaining = store.list_user_accounts(int(user["id"]))
    # Don't allow deleting the last account — every user needs at least one to
    # have a valid scope for runs/contributions.
    if len(remaining) <= 1:
        return RedirectResponse(url="/accounts", status_code=303)
    store.delete_user_account(int(user["id"]), account_id)
    return RedirectResponse(url="/accounts", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    account = ctx["current_account_id"]
    ctx["last_run"] = store.last_run(account=account)
    ctx["recent_runs"] = store.list_runs(limit=5, account=account)
    ctx["active_jobs"] = store.list_active_jobs(account=account, limit=10)
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    return templates.TemplateResponse(request, "upload.html", ctx)


@app.post("/upload", response_class=HTMLResponse)
async def upload_post(request: Request, file: UploadFile = File(...),
                      user: Dict[str, Any] = Depends(require_user)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8", errors="replace")
        portfolio = parse_fidelity_csv(text)
    except Exception as e:  # noqa: BLE001
        store = get_store()
        ctx = _base_ctx(request, store, user=user)
        ctx["error"] = f"Failed to parse CSV: {e}"
        return templates.TemplateResponse(request, "upload.html", ctx)

    if not portfolio.holdings and portfolio.cash <= 0:
        store = get_store()
        ctx = _base_ctx(request, store, user=user)
        ctx["error"] = "Parsed 0 holdings and $0 cash — is this a Fidelity Portfolio_Positions CSV?"
        return templates.TemplateResponse(request, "upload.html", ctx)

    token = secrets.token_urlsafe(16)
    _pending_portfolios[token] = portfolio

    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    ctx["portfolio"] = portfolio
    ctx["token"] = token
    return templates.TemplateResponse(request, "upload_preview.html", ctx)


@app.post("/runs/{run_id}/delete")
def delete_run(request: Request, run_id: int,
               user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    account = _current_account(request, user, store)
    store.delete_run(run_id, account=account)
    return RedirectResponse(url="/history", status_code=303)


@app.get("/about", response_class=HTMLResponse)
def about(request: Request, user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    return templates.TemplateResponse(request, "about.html", ctx)


@app.post("/run")
def run_now(request: Request, token: str = Form(...), force: str = Form(""),
            force_rebalance: str = Form(""),
            user: Dict[str, Any] = Depends(require_user)):
    pf = _pending_portfolios.pop(token, None)
    if pf is None:
        raise HTTPException(404, "Pending portfolio not found — please re-upload.")
    store = get_store()
    account = _current_account(request, user, store)
    enable_ai = _ai_enabled_for_user(user, store)
    job_id = store.create_job(account=account, step_total=5, step_name="Queued",
                              message="Waiting for worker thread...")
    _advisor_executor.submit(
        _run_advisor_job,
        job_id, account, pf,
        (force == "1"),
        (force_rebalance == "1"),
        int(user["id"]),
        enable_ai,
    )
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int,
               user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    account = _current_account(request, user, store)
    job = store.get_job(job_id, account=account)
    if job is None:
        raise HTTPException(404, "Job not found")
    ctx = _base_ctx(request, store, user=user)
    ctx["job"] = job
    return templates.TemplateResponse(request, "job_detail.html", ctx)


@app.get("/api/jobs/{job_id}")
def api_job_status(request: Request, job_id: int,
                   user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    account = _current_account(request, user, store)
    job = store.get_job(job_id, account=account)
    if job is None:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.websocket("/ws/jobs/{job_id}")
async def ws_job_status(websocket: WebSocket, job_id: int):
    # WS path: authenticate via the session cookie and scope to that user's
    # account. Reject the upgrade if not logged in or job is not theirs.
    session_token = websocket.cookies.get(SESSION_COOKIE) or ""
    store = Store()
    user = store.get_session_user(session_token) if session_token else None
    if user is None:
        await websocket.close(code=4401)
        return
    cookie_account = websocket.cookies.get("account") or ""
    if cookie_account and store.user_owns_account_slug(int(user["id"]), cookie_account):
        account = cookie_account
    else:
        accts = store.list_user_accounts(int(user["id"]))
        account = accts[0]["slug"] if accts else DEFAULT_ACCOUNT
    job = store.get_job(job_id, account=account)
    if job is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    last_version = -1
    try:
        while True:
            job = store.get_job(job_id, account=account)
            if job is None:
                await websocket.send_json({"status": "missing"})
                break
            # Only push when version changed (avoids spamming idle ticks)
            if int(job.get("version") or 0) != last_version:
                last_version = int(job.get("version") or 0)
                await websocket.send_json(job)
            if job.get("status") in ("done", "failed", "pushback"):
                # Allow the client a moment to receive the terminal frame, then close
                await asyncio.sleep(0.2)
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        _log.warning("WS for job %d errored: %s", job_id, e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/cadence_warning", response_class=HTMLResponse)
def cadence_warning(request: Request, token: str, reason: str,
                    force_rebalance: str = "",
                    user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    ctx["token"] = token
    ctx["reason"] = reason
    ctx["force_rebalance"] = force_rebalance
    return templates.TemplateResponse(request, "cadence_warning.html", ctx)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int,
               user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    account = _current_account(request, user, store)
    rec = store.get_run(run_id, account=account)
    if rec is None:
        raise HTTPException(404, "Run not found")
    ctx = _base_ctx(request, store, user=user)
    ctx["run"] = rec
    ctx["orders"] = rec.get("orders") or []
    ctx["ai_review"] = rec.get("ai_review")
    ctx["holdings_before"] = rec.get("holdings_before") or {}
    ctx["holdings_target"] = rec.get("holdings_target") or {}
    ctx["price_snapshot"] = rec.get("price_snapshot") or {}
    ctx["cost_basis_by_ticker"] = rec.get("cost_basis_by_ticker") or {}
    engine_sectors: Dict[str, str] = rec.get("candidate_sectors") or {}
    # Per-ticker AI lookup for the orders table
    ai_by_ticker: Dict[str, Dict[str, Any]] = {}
    if rec.get("ai_review") and isinstance(rec["ai_review"], dict):
        for item in rec["ai_review"].get("per_order", []) or []:
            ai_by_ticker[item["ticker"]] = item
    ctx["ai_by_ticker"] = ai_by_ticker

    # Sector / category breakdown of current holdings (by market value).
    holdings_before = rec.get("holdings_before") or {}
    price_snapshot = rec.get("price_snapshot") or {}
    cash = float(rec.get("cash") or 0)

    def _breakdown(holdings_map: Dict[str, float], cash_amt: float) -> List[Dict[str, Any]]:
        totals: Dict[str, float] = {}
        for tkr, shares in holdings_map.items():
            px = price_snapshot.get(tkr) or 0
            val = float(shares) * float(px)
            if val <= 0:
                continue
            sec = engine_sectors.get(tkr) or _ticker_sector(tkr) or "Unknown"
            if sec == "Unknown":
                # Last-resort yfinance lookup so the UI doesn't bucket REITs
                # / Bonds / etc. under "Unknown" when the engine didn't have
                # a classification for a held name.
                sec = _ticker_sector(tkr) or "Unknown"
            totals[sec] = totals.get(sec, 0.0) + val
        if cash_amt > 0:
            totals["Cash"] = totals.get("Cash", 0.0) + cash_amt
        tot = sum(totals.values()) or 1.0
        return sorted(
            [{"sector": s, "value": v, "pct": v / tot * 100} for s, v in totals.items()],
            key=lambda r: r["value"], reverse=True,
        )

    ctx["sector_breakdown"] = _breakdown(holdings_before, cash)

    # After-orders projection: apply BUY/SELL net deltas to holdings_before so the
    # user can see how today's marching orders shift the sector mix.
    orders_list = rec.get("orders") or []
    if orders_list:
        proj_holdings: Dict[str, float] = {k: float(v) for k, v in holdings_before.items()}
        proj_cash = cash
        for o in orders_list:
            tkr = o.get("ticker")
            shares = float(o.get("shares") or 0)
            dollars = float(o.get("dollar_value") or 0)
            if not tkr or shares <= 0:
                continue
            if o.get("action") == "BUY":
                proj_holdings[tkr] = proj_holdings.get(tkr, 0.0) + shares
                proj_cash -= dollars
            elif o.get("action") == "SELL":
                proj_holdings[tkr] = proj_holdings.get(tkr, 0.0) - shares
                proj_cash += dollars
        ctx["sector_breakdown_after"] = _breakdown(proj_holdings, max(0.0, proj_cash))
    else:
        ctx["sector_breakdown_after"] = None

    return templates.TemplateResponse(request, "run_detail.html", ctx)


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    ctx["runs"] = store.list_runs(limit=200, account=ctx["current_account_id"])
    return templates.TemplateResponse(request, "history.html", ctx)


@app.post("/contributions/{quarter}/deposited")
def mark_deposited(request: Request, quarter: str,
                   user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    store.mark_contribution_deposited(
        quarter, account=_current_account(request, user, store))
    return RedirectResponse(url="/", status_code=303)


@app.post("/account/switch")
def account_switch(request: Request, account: str = Form(...),
                   user: Dict[str, Any] = Depends(require_user)):
    target = request.headers.get("referer") or "/"
    response = RedirectResponse(url=target, status_code=303)
    store = get_store()
    if store.user_owns_account_slug(int(user["id"]), account):
        response.set_cookie("account", account, max_age=60 * 60 * 24 * 365,
                            httponly=False, samesite="lax")
    return response


@app.get("/benchmark", response_class=HTMLResponse)
def benchmark(request: Request, user: Dict[str, Any] = Depends(require_user)):
    store = get_store()
    account = _current_account(request, user, store)
    runs = store.list_runs(limit=500, account=account)
    runs_chrono = list(reversed(runs))
    portfolio_series = [{"date": r["asof_date"], "value": r["portfolio_value"]} for r in runs_chrono]
    ctx = _base_ctx(request, store, user=user)
    ctx["runs"] = runs_chrono
    ctx["portfolio_series"] = portfolio_series
    bench: Dict[str, list] = {}
    if runs_chrono:
        try:
            bench = _simulate_benchmarks(runs_chrono)
        except Exception as e:  # noqa: BLE001
            bench = {}
            ctx["benchmark_error"] = f"{type(e).__name__}: {e}"
    ctx["benchmark_series"] = bench
    # Drawdown stats: portfolio (from run history) and SPY (from latest run record)
    port_vals = [r["portfolio_value"] for r in runs_chrono if r.get("portfolio_value")]
    port_peak = max(port_vals) if port_vals else 0.0
    port_dd_cur = ((port_vals[-1] - port_peak) / port_peak * 100) if port_peak else 0.0
    port_dd_max = 0.0
    running = 0.0
    for v in port_vals:
        running = max(running, v)
        if running:
            port_dd_max = min(port_dd_max, (v - running) / running * 100)
    spy_dd_cur = (runs_chrono[-1].get("spy_drawdown") or 0.0) * 100 if runs_chrono else 0.0
    ctx["dd_stats"] = {
        "port_cur": round(port_dd_cur, 2),
        "port_max": round(port_dd_max, 2),
        "spy_cur": round(spy_dd_cur, 2),
    }
    return templates.TemplateResponse(request, "benchmark.html", ctx)


def _simulate_benchmarks(runs_chrono):
    """Simulate apples-to-apples benchmark mixes against actual run dates.

    Rules (mirror the user's strategy):
      - Seed each benchmark with the user's first portfolio_value on the first run date.
      - Between run dates: mark-to-market using ETF price ratios.
      - In contribution months (Jan/Apr/Jul/Oct): add $1,750 split per TARGET weights.
      - In February: rebalance back to target weights.
    """
    from _signal_engine import get_etf_series
    import pandas as pd

    etfs = get_etf_series()
    mixes = [
        ("SSO/BND 70/30", {"SSO": 0.70, "BND": 0.30}),
        ("VTI/BND 70/30", {"VTI": 0.70, "BND": 0.30}),
        ("SSO/VTI/BND 50/30/20", {"SSO": 0.50, "VTI": 0.30, "BND": 0.20}),
    ]
    run_dates = [pd.Timestamp(r["asof_date"]) for r in runs_chrono]
    seed = float(runs_chrono[0]["portfolio_value"])
    CONTRIB = 1750.0
    CONTRIB_MONTHS = {1, 4, 7, 10}
    REBAL_MONTH = 2

    # Pre-resample each ETF series to month-end
    price_series = {}
    for _, mix in mixes:
        for t in mix:
            if t not in price_series:
                price_series[t] = etfs[t].resample("BME").last()

    def price_at(t: str, dt: pd.Timestamp) -> float:
        s = price_series[t]
        sub = s.loc[:dt]
        return float(sub.iloc[-1]) if len(sub) else float("nan")

    out: Dict[str, list] = {}
    for label, weights in mixes:
        pos: Dict[str, float] = {t: seed * w for t, w in weights.items()}
        points = [{"date": str(run_dates[0].date()), "value": round(sum(pos.values()), 2)}]
        prev = run_dates[0]
        for dt in run_dates[1:]:
            # mark-to-market
            for t in pos:
                p_prev = price_at(t, prev)
                p_now = price_at(t, dt)
                if p_prev and p_now and p_prev > 0:
                    pos[t] *= (p_now / p_prev)
            # quarterly contribution
            if dt.month in CONTRIB_MONTHS:
                for t, w in weights.items():
                    pos[t] += CONTRIB * w
            # Feb rebalance
            if dt.month == REBAL_MONTH:
                total = sum(pos.values())
                for t, w in weights.items():
                    pos[t] = total * w
            points.append({"date": str(dt.date()), "value": round(sum(pos.values()), 2)})
            prev = dt
        out[label] = points
    return out


@app.get("/backtest", response_class=HTMLResponse)
def backtest_form(request: Request, user: Dict[str, Any] = Depends(require_user)):
    from advisor.backtest import STRATEGIES, MAX_STRATEGIES_PER_RUN
    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    ctx["strategies"] = STRATEGIES
    ctx["max_strategies"] = MAX_STRATEGIES_PER_RUN
    ctx["results"] = None
    ctx["form_defaults"] = {
        "years": 13,
        "init": 10000,
        "annual_contrib": 7000,
        "cadence": "Q",
        "selected": ["pure_div_champion", "sso_bnd_70_30"],
    }
    return templates.TemplateResponse(request, "backtest.html", ctx)


@app.post("/backtest", response_class=HTMLResponse)
async def backtest_run(request: Request, user: Dict[str, Any] = Depends(require_user)):
    from advisor.backtest import (
        MAX_STRATEGIES_PER_RUN,
        STRATEGIES,
        VALID_CADENCES,
        run_backtest_set,
    )
    form = await request.form()
    try:
        years = int(form.get("years", 13))
        init = float(form.get("init", 10000))
        annual_contrib = float(form.get("annual_contrib", 7000))
        cadence = str(form.get("cadence", "Q"))
        strategy_ids = form.getlist("strategy")
    except (TypeError, ValueError) as e:
        store = get_store()
        ctx = _base_ctx(request, store, user=user)
        ctx["strategies"] = STRATEGIES
        ctx["max_strategies"] = MAX_STRATEGIES_PER_RUN
        ctx["form_defaults"] = {"years": 13, "init": 10000, "annual_contrib": 7000,
                                "cadence": "Q", "selected": []}
        ctx["results"] = None
        ctx["error"] = f"Invalid form input: {e}"
        return templates.TemplateResponse(request, "backtest.html", ctx)

    years = max(1, min(20, years))
    init = max(0.0, init)
    annual_contrib = max(0.0, annual_contrib)
    if cadence not in VALID_CADENCES:
        cadence = "Q"

    store = get_store()
    ctx = _base_ctx(request, store, user=user)
    ctx["strategies"] = STRATEGIES
    ctx["max_strategies"] = MAX_STRATEGIES_PER_RUN
    ctx["form_defaults"] = {
        "years": years, "init": init, "annual_contrib": annual_contrib,
        "cadence": cadence, "selected": strategy_ids,
    }
    try:
        results, run_info = run_backtest_set(strategy_ids, years, init, annual_contrib, cadence)
        ctx["results"] = results
        ctx["run_info"] = run_info
    except ValueError as e:
        ctx["results"] = None
        ctx["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        ctx["results"] = None
        ctx["error"] = f"Backtest failed: {type(e).__name__}: {e}"
    return templates.TemplateResponse(request, "backtest.html", ctx)


@app.get("/health")
def health():
    return {"status": "ok", "ai_enabled": _ai_globally_enabled()}


@app.get("/api/strategy")
def api_strategy():
    """Live strategy metadata. UI templates source the same dict via the
    `strategy` Jinja global, so anything shown to the user matches what the
    engine is actually running."""
    return strategy_info()
