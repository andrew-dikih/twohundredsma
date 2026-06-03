"""State store for the portfolio advisor.

Two backends are supported and selected by env var:

  COSMOS_CONNECTION_STRING set -> Azure Cosmos DB (NoSQL, ``Documents``
    container partitioned by ``/documentType``). Used in Azure deployments.
  Otherwise                    -> SQLite. Used for local Docker development.

Both backends expose the same ``Store`` interface (same method names and
return shapes) so the rest of the app is backend-agnostic. ``Store()`` is a
module-level factory function that returns whichever backend is active.

Schema:
    runs            -- one row per advisor run
    contributions   -- IRA contribution ledger (quarterly $1,750)
    settings        -- key-value (last_peak_spy, etc.)
    jobs            -- async advisor jobs (progress + websocket fan-out)
    users           -- registered accounts (auth)
    sessions        -- session cookies -> user id
    user_accounts   -- portfolios a user owns
    ai_usage        -- per-user-per-day AI quota log

SQLite DB defaults to /data/advisor.db inside docker; falls back to
./advisor_data/advisor.db locally if /data isn't writable.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATHS = ("/data/advisor.db", "./advisor_data/advisor.db")


def resolve_db_path(explicit: Optional[str] = None) -> str:
    if explicit:
        Path(explicit).parent.mkdir(parents=True, exist_ok=True)
        return explicit
    for p in DEFAULT_DB_PATHS:
        try:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            test = Path(p).parent / ".write_test"
            test.write_text("ok")
            test.unlink()
            return p
        except (OSError, PermissionError):
            continue
    fallback = "./advisor_data/advisor.db"
    Path(fallback).parent.mkdir(parents=True, exist_ok=True)
    return fallback


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'andrew_roth_ira',  -- account this run belongs to
    run_ts TEXT NOT NULL,                  -- ISO datetime when run executed
    asof_date TEXT NOT NULL,               -- portfolio CSV asof date
    run_type TEXT NOT NULL,                -- 'rebalance' | 'dip_buy' | 'snapback' | 'hold' | 'contribution' | 'mixed'
    portfolio_value REAL NOT NULL,
    cash REAL NOT NULL,
    stocks_pct REAL,
    bnd_pct REAL,
    last_peak_spy REAL,
    current_spy REAL,
    spy_drawdown REAL,
    orders_json TEXT NOT NULL,
    holdings_before_json TEXT NOT NULL,
    holdings_target_json TEXT NOT NULL,
    price_snapshot_json TEXT,              -- ticker -> price as of run
    cost_basis REAL,                       -- total cost basis of equity holdings (from CSV)
    cost_basis_by_ticker_json TEXT,        -- ticker -> cost basis total (from CSV)
    ai_status TEXT,                        -- 'full' | 'partial' | 'disabled' | 'failed'
    ai_review_json TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_asof ON runs(asof_date);

CREATE TABLE IF NOT EXISTS contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'andrew_roth_ira',
    quarter TEXT NOT NULL,                 -- e.g. '2026-Q2'
    expected_amount REAL NOT NULL,
    deposited INTEGER NOT NULL DEFAULT 0,  -- bool 0/1
    deposited_date TEXT,
    UNIQUE (account, quarter)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'advisor',         -- future: backtest, etc.
    status TEXT NOT NULL DEFAULT 'queued',        -- queued | running | done | failed | pushback
    step_idx INTEGER NOT NULL DEFAULT 0,
    step_total INTEGER NOT NULL DEFAULT 1,
    step_name TEXT,
    message TEXT,
    progress_pct REAL NOT NULL DEFAULT 0.0,       -- 0..100
    started_at TEXT NOT NULL,
    finished_at TEXT,
    run_id INTEGER,                               -- FK to runs (set when done)
    redirect_url TEXT,                            -- set on pushback (cadence warning)
    error TEXT,
    version INTEGER NOT NULL DEFAULT 0            -- bumped on every update; used by WS to dedupe
);
CREATE INDEX IF NOT EXISTS idx_jobs_account_status ON jobs(account, status);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'regular',  -- 'regular' | 'admin'
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, slug),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_accounts_user ON user_accounts(user_id);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    used_date TEXT NOT NULL,   -- 'YYYY-MM-DD' UTC
    run_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_user_date ON ai_usage(user_id, used_date);
"""

# Legacy hardcoded accounts: seeded to the first admin user as their initial accounts
# so the user doesn't lose access to all the runs that already exist under these slugs.
LEGACY_ACCOUNT_SEED: List[tuple] = [
    ("andrew_roth_ira", "Andrew Roth IRA"),
    ("jim_roth_ira",    "Jim Roth IRA"),
    ("debby_roth_ira",  "Debby Roth IRA"),
    ("jim_hsa",         "Jim HSA"),
    ("jim_roth_401k",   "Jim Roth 401k"),
    ("jim_401k",        "Jim 401k"),
]


DEFAULT_ACCOUNT = "andrew_roth_ira"


def _slugify(label: str) -> str:
    """Coerce free-form label into a stable slug used in URLs/cookies/DB."""
    import re
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return s or "account"


@dataclass
class RunRecord:
    id: Optional[int]
    run_ts: str
    asof_date: str
    run_type: str
    portfolio_value: float
    cash: float
    stocks_pct: Optional[float]
    bnd_pct: Optional[float]
    last_peak_spy: Optional[float]
    current_spy: Optional[float]
    spy_drawdown: Optional[float]
    orders: List[Dict[str, Any]] = field(default_factory=list)
    holdings_before: Dict[str, float] = field(default_factory=dict)
    holdings_target: Dict[str, float] = field(default_factory=dict)
    price_snapshot: Dict[str, float] = field(default_factory=dict)
    cost_basis: Optional[float] = None
    cost_basis_by_ticker: Dict[str, float] = field(default_factory=dict)
    ai_status: Optional[str] = None
    ai_review: Optional[Dict[str, Any]] = None
    notes: str = ""
    account: str = DEFAULT_ACCOUNT


class SqliteStore:
    """SQLite-backed Store. Use the module-level ``Store()`` factory rather
    than instantiating this class directly so the active backend can be
    swapped via environment variable."""
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = resolve_db_path(db_path)
        self._init_schema()

    # --- core ---
    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10.0)
        c.row_factory = sqlite3.Row
        # PRAGMAs run outside a transaction. Safe to call every connect; SQLite
        # is fast on these.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Add new columns to existing DBs (idempotent)
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)").fetchall()}
            if "price_snapshot_json" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN price_snapshot_json TEXT")
            if "cost_basis" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN cost_basis REAL")
            if "cost_basis_by_ticker_json" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN cost_basis_by_ticker_json TEXT")
            if "account" not in cols:
                c.execute(
                    f"ALTER TABLE runs ADD COLUMN account TEXT NOT NULL DEFAULT '{DEFAULT_ACCOUNT}'"
                )
            c.execute("CREATE INDEX IF NOT EXISTS idx_runs_account ON runs(account)")
            # Same for contributions
            ccols = {r[1] for r in c.execute("PRAGMA table_info(contributions)").fetchall()}
            if "account" not in ccols:
                c.execute(
                    f"ALTER TABLE contributions ADD COLUMN account TEXT NOT NULL DEFAULT '{DEFAULT_ACCOUNT}'"
                )
                # The legacy UNIQUE(quarter) constraint can't be dropped via ALTER;
                # leave it and rely on (account, quarter) lookups using an explicit
                # WHERE clause. New rows for non-default accounts must use a unique
                # synthetic quarter key when colliding (we tolerate the legacy UNIQUE
                # by suffixing in upsert when needed — see upsert_contribution).

    # --- settings ---
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key,value,updated_ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (key, value, datetime.utcnow().isoformat()),
            )

    def get_last_peak_spy(self) -> Optional[float]:
        v = self.get_setting("last_peak_spy")
        return float(v) if v else None

    def set_last_peak_spy(self, v: float) -> None:
        self.set_setting("last_peak_spy", str(v))

    # --- contributions (account-scoped) ---
    def get_contribution(self, quarter: str, account: str = DEFAULT_ACCOUNT) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM contributions WHERE quarter=? AND account=?",
                (quarter, account),
            ).fetchone()
            return dict(r) if r else None

    def upsert_contribution(self, quarter: str, expected_amount: float = 1750.0,
                            account: str = DEFAULT_ACCOUNT) -> None:
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM contributions WHERE quarter=? AND account=?",
                (quarter, account),
            ).fetchone()
            if existing:
                return
            # Legacy table had UNIQUE(quarter); for non-default accounts we
            # synthesize a distinct quarter-like key to avoid the legacy
            # constraint when present. Once that legacy constraint is gone
            # (fresh DB created with the new SCHEMA) the synthesis is harmless.
            qkey = quarter if account == DEFAULT_ACCOUNT else f"{account}:{quarter}"
            try:
                c.execute(
                    "INSERT INTO contributions(account, quarter, expected_amount, deposited) VALUES(?,?,?,0)",
                    (account, qkey, expected_amount),
                )
            except sqlite3.IntegrityError:
                # Fallback: row exists under the synthetic key already
                pass

    def mark_contribution_deposited(self, quarter: str, when: Optional[date] = None,
                                    account: str = DEFAULT_ACCOUNT) -> None:
        when = when or date.today()
        qkey = quarter if account == DEFAULT_ACCOUNT else f"{account}:{quarter}"
        with self._conn() as c:
            c.execute(
                "UPDATE contributions SET deposited=1, deposited_date=? WHERE quarter=? AND account=?",
                (when.isoformat(), qkey, account),
            )

    def list_contributions(self, account: str = DEFAULT_ACCOUNT) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM contributions WHERE account=? ORDER BY quarter DESC", (account,)).fetchall()]
        # Strip the synthetic prefix from the quarter string for display
        for r in rows:
            q = r.get("quarter") or ""
            prefix = f"{account}:"
            if q.startswith(prefix):
                r["quarter"] = q[len(prefix):]
        return rows

    # --- runs (account-scoped) ---
    def insert_run(self, rec: RunRecord) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO runs(
                    account, run_ts, asof_date, run_type, portfolio_value, cash, stocks_pct, bnd_pct,
                    last_peak_spy, current_spy, spy_drawdown,
                    orders_json, holdings_before_json, holdings_target_json, price_snapshot_json,
                    cost_basis, cost_basis_by_ticker_json, ai_status, ai_review_json, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.account, rec.run_ts, rec.asof_date, rec.run_type, rec.portfolio_value, rec.cash,
                    rec.stocks_pct, rec.bnd_pct, rec.last_peak_spy, rec.current_spy, rec.spy_drawdown,
                    json.dumps(rec.orders), json.dumps(rec.holdings_before), json.dumps(rec.holdings_target),
                    json.dumps(rec.price_snapshot) if rec.price_snapshot else None,
                    rec.cost_basis,
                    json.dumps(rec.cost_basis_by_ticker) if rec.cost_basis_by_ticker else None,
                    rec.ai_status, json.dumps(rec.ai_review) if rec.ai_review else None, rec.notes,
                ),
            )
            return int(cur.lastrowid)

    def get_run(self, run_id: int, account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            if account is None:
                r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            else:
                r = c.execute("SELECT * FROM runs WHERE id=? AND account=?",
                              (run_id, account)).fetchone()
            return _row_to_run_dict(r) if r else None

    def list_runs(self, limit: int = 100, account: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as c:
            if account is None:
                rows = c.execute(
                    "SELECT * FROM runs ORDER BY run_ts DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM runs WHERE account=? ORDER BY run_ts DESC LIMIT ?",
                    (account, limit)).fetchall()
            return [_row_to_run_dict(r) for r in rows]

    def last_run(self, account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            if account is None:
                r = c.execute("SELECT * FROM runs ORDER BY run_ts DESC LIMIT 1").fetchone()
            else:
                r = c.execute(
                    "SELECT * FROM runs WHERE account=? ORDER BY run_ts DESC LIMIT 1",
                    (account,)).fetchone()
            return _row_to_run_dict(r) if r else None

    def delete_run(self, run_id: int, account: Optional[str] = None) -> bool:
        with self._conn() as c:
            if account is None:
                cur = c.execute("DELETE FROM runs WHERE id=?", (run_id,))
            else:
                cur = c.execute("DELETE FROM runs WHERE id=? AND account=?",
                                (run_id, account))
            return cur.rowcount > 0

    # --- jobs (account-scoped) ---
    def create_job(self, account: str, kind: str = "advisor",
                   step_total: int = 5, step_name: str = "Queued",
                   message: str = "Waiting for worker...") -> int:
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO jobs(account, kind, status, step_idx, step_total, "
                "step_name, message, progress_pct, started_at, version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (account, kind, "queued", 0, step_total, step_name, message,
                 0.0, ts, 0),
            )
            return int(cur.lastrowid)

    def get_job(self, job_id: int, account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            if account is None:
                r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            else:
                r = c.execute("SELECT * FROM jobs WHERE id=? AND account=?",
                              (job_id, account)).fetchone()
            return dict(r) if r else None

    def list_active_jobs(self, account: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Jobs that are queued or running (per-account)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE account=? AND status IN ('queued','running') "
                "ORDER BY started_at DESC LIMIT ?",
                (account, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_recent_jobs(self, account: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE account=? ORDER BY started_at DESC LIMIT ?",
                (account, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_job_progress(self, job_id: int, step_idx: int, step_total: int,
                            step_name: str, message: str,
                            progress_pct: Optional[float] = None,
                            status: Optional[str] = None) -> None:
        """Best-effort progress update. Caller wraps in try/except.

        If progress_pct is None, derives it from step_idx/step_total.
        If status is None, leaves status unchanged (separately set running once).
        """
        if progress_pct is None:
            denom = max(1, step_total)
            progress_pct = round(100.0 * step_idx / denom, 1)
        with self._conn() as c:
            if status:
                c.execute(
                    "UPDATE jobs SET step_idx=?, step_total=?, step_name=?, message=?, "
                    "progress_pct=?, status=?, version=version+1 WHERE id=?",
                    (step_idx, step_total, step_name, message, progress_pct, status, job_id),
                )
            else:
                c.execute(
                    "UPDATE jobs SET step_idx=?, step_total=?, step_name=?, message=?, "
                    "progress_pct=?, version=version+1 WHERE id=?",
                    (step_idx, step_total, step_name, message, progress_pct, job_id),
                )

    def mark_job_running(self, job_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status='running', step_name='Starting', "
                "message='Worker picked up the job', version=version+1 WHERE id=?",
                (job_id,),
            )

    def mark_job_done(self, job_id: int, run_id: int) -> None:
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status='done', step_idx=step_total, progress_pct=100.0, "
                "step_name='Complete', message='Run finished', finished_at=?, run_id=?, "
                "version=version+1 WHERE id=?",
                (ts, run_id, job_id),
            )

    def mark_job_failed(self, job_id: int, error: str) -> None:
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status='failed', step_name='Failed', message=?, "
                "finished_at=?, error=?, version=version+1 WHERE id=?",
                (error[:500], ts, error[:2000], job_id),
            )

    def mark_job_pushback(self, job_id: int, reason: str, redirect_url: str) -> None:
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status='pushback', step_name='Cadence warning', "
                "message=?, finished_at=?, redirect_url=?, version=version+1 WHERE id=?",
                (reason[:500], ts, redirect_url, job_id),
            )

    def cleanup_stale_jobs(self) -> int:
        """Mark queued/running jobs as failed on app startup (interrupted by restart)."""
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status='failed', step_name='Interrupted', "
                "message='Interrupted by app restart; please re-upload portfolio.', "
                "finished_at=?, error='app_restart', version=version+1 "
                "WHERE status IN ('queued','running')",
                (ts,),
            )
            return int(cur.rowcount)

    # --- users / auth ---
    def create_user(self, username: str, password_hash: str,
                    user_type: str = "regular") -> int:
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO users(username, password_hash, type, created_at) "
                "VALUES(?,?,?,?)",
                (username.strip(), password_hash, user_type, ts),
            )
            uid = int(cur.lastrowid)
        # If this is the first admin, claim the legacy account slugs so existing
        # runs/contributions remain visible. Idempotent.
        if user_type == "admin":
            self._seed_legacy_accounts_for_user(uid)
        return uid

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
            return dict(r) if r else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(r) if r else None

    def admin_exists(self) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT 1 FROM users WHERE type='admin' LIMIT 1").fetchone()
            return r is not None

    # --- sessions ---
    def create_session(self, user_id: int, token: str, ttl_days: int = 30) -> None:
        ts = datetime.utcnow()
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions(token, user_id, created_at, expires_at) "
                "VALUES(?,?,?,?)",
                (token, user_id, ts.isoformat(),
                 (ts.replace(microsecond=0).isoformat()
                  if ttl_days <= 0 else
                  (ts + _td_days(ttl_days)).isoformat())),
            )

    def get_session_user(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._conn() as c:
            r = c.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires_at > ?",
                (token, datetime.utcnow().isoformat()),
            ).fetchone()
            return dict(r) if r else None

    def delete_session(self, token: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))

    def cleanup_expired_sessions(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (datetime.utcnow().isoformat(),),
            )
            return int(cur.rowcount)

    # --- user accounts ---
    def _seed_legacy_accounts_for_user(self, user_id: int) -> None:
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            for slug, label in LEGACY_ACCOUNT_SEED:
                # INSERT OR IGNORE -> idempotent if admin re-registers later
                c.execute(
                    "INSERT OR IGNORE INTO user_accounts(user_id, slug, label, created_at) "
                    "VALUES(?,?,?,?)",
                    (user_id, slug, label, ts),
                )

    def list_user_accounts(self, user_id: int) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, user_id, slug, label, created_at "
                "FROM user_accounts WHERE user_id=? ORDER BY id ASC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def create_user_account(self, user_id: int, label: str,
                            slug: Optional[str] = None) -> Dict[str, Any]:
        label = (label or "").strip()
        if not label:
            raise ValueError("Account label is required")
        base = _slugify(slug or label)
        # Personal namespace prefix avoids collisions across users sharing slugs
        slug_final = f"u{user_id}_{base}"
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            # On collision (user re-uses same label), append numeric suffix
            n = 1
            candidate = slug_final
            while c.execute(
                "SELECT 1 FROM user_accounts WHERE user_id=? AND slug=?",
                (user_id, candidate),
            ).fetchone():
                n += 1
                candidate = f"{slug_final}_{n}"
            c.execute(
                "INSERT INTO user_accounts(user_id, slug, label, created_at) "
                "VALUES(?,?,?,?)",
                (user_id, candidate, label, ts),
            )
            r = c.execute(
                "SELECT id, user_id, slug, label, created_at "
                "FROM user_accounts WHERE user_id=? AND slug=?",
                (user_id, candidate),
            ).fetchone()
            return dict(r)

    def get_user_account_by_slug(self, user_id: int, slug: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute(
                "SELECT id, user_id, slug, label, created_at "
                "FROM user_accounts WHERE user_id=? AND slug=?",
                (user_id, slug),
            ).fetchone()
            return dict(r) if r else None

    def delete_user_account(self, user_id: int, account_id: int) -> bool:
        """Delete a user_accounts row. Note: does NOT cascade to runs/contributions/jobs
        — those keep their slug-based scoping in case the user re-creates the account."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM user_accounts WHERE id=? AND user_id=?",
                (account_id, user_id),
            )
            return cur.rowcount > 0

    def user_owns_account_slug(self, user_id: int, slug: str) -> bool:
        return self.get_user_account_by_slug(user_id, slug) is not None

    # --- AI usage quota ---
    def ai_used_today(self, user_id: int, today: Optional[date] = None) -> bool:
        d = (today or date.today()).isoformat()
        with self._conn() as c:
            r = c.execute(
                "SELECT 1 FROM ai_usage WHERE user_id=? AND used_date=? LIMIT 1",
                (user_id, d),
            ).fetchone()
            return r is not None

    def ai_usage_count_today(self, user_id: int, today: Optional[date] = None) -> int:
        d = (today or date.today()).isoformat()
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) AS n FROM ai_usage WHERE user_id=? AND used_date=?",
                (user_id, d),
            ).fetchone()
            return int(r["n"] if r else 0)

    def log_ai_usage(self, user_id: int, run_id: Optional[int] = None,
                     when: Optional[date] = None) -> None:
        d = (when or date.today()).isoformat()
        ts = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                "INSERT INTO ai_usage(user_id, used_date, run_id, created_at) "
                "VALUES(?,?,?,?)",
                (user_id, d, run_id, ts),
            )


def _td_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _row_to_run_dict(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    for k in ("orders_json", "holdings_before_json", "holdings_target_json",
              "price_snapshot_json", "cost_basis_by_ticker_json", "ai_review_json"):
        if d.get(k):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                d[k.replace("_json", "")] = None
    return d


def quarter_of(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


def is_contribution_month(d: date) -> bool:
    return d.month in (1, 4, 7, 10)


# --- Backend selection ------------------------------------------------------
# Cache the active backend instance so callers can do ``Store()`` cheaply
# anywhere without paying connection overhead per call. For SQLite the cost
# is small but for Cosmos the SDK strongly prefers a long-lived client.

_BACKEND_SINGLETON: Optional[Any] = None


def _make_backend() -> Any:
    conn = os.getenv("COSMOS_CONNECTION_STRING", "").strip()
    if conn:
        # Import lazily so local dev doesn't require azure-cosmos installed.
        from advisor.store_cosmos import CosmosStore  # noqa: WPS433 (local import is intentional)
        db_name = os.getenv("COSMOS_DATABASE", "TwoHundredSMA")
        container_name = os.getenv("COSMOS_CONTAINER", "Documents")
        return CosmosStore(connection_string=conn,
                           database_name=db_name,
                           container_name=container_name)
    return SqliteStore()


def Store(db_path: Optional[str] = None):  # noqa: N802 (intentionally PascalCase to look like a class)
    """Return the active Store backend.

    The first call decides the backend (Cosmos if ``COSMOS_CONNECTION_STRING``
    is set, otherwise SQLite) and caches it; subsequent calls return the
    cached instance. ``db_path`` is honoured only for SQLite and only on the
    first call (it short-circuits backend selection to SQLite, useful for
    tests and the optional one-off migration script).
    """
    global _BACKEND_SINGLETON
    if db_path is not None:
        return SqliteStore(db_path)
    if _BACKEND_SINGLETON is None:
        _BACKEND_SINGLETON = _make_backend()
    return _BACKEND_SINGLETON


def _reset_store_singleton_for_tests() -> None:
    """Test helper: drop the cached backend so the next ``Store()`` re-reads env."""
    global _BACKEND_SINGLETON
    _BACKEND_SINGLETON = None
