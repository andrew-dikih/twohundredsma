"""Azure Cosmos DB backend for the portfolio advisor.

Single container ``Documents`` partitioned by ``/documentType``. Document
shapes by ``documentType``:

  user            id="user_username:{lc}"       fields: seq, username,
                  username_lc, password_hash, type, created_at
  session         id=<token>                    fields: user_id (seq),
                  created_at, expires_at, ttl
  user_account    id="useracct:{user_id}:{slug}" fields: seq, user_id, slug,
                  label, created_at
  ai_usage        id="aiu:{user_id}:{date}"     fields: user_id, used_date,
                  run_id, created_at, ttl
  run             id=str(seq)                   fields: account, run_ts,
                  asof_date, run_type, ...JSON columns as nested dicts
  job             id=str(seq)                   fields: account, kind, status,
                  step_idx, step_total, step_name, message, progress_pct,
                  started_at, finished_at, run_id, redirect_url, error,
                  version
  contribution    id="contrib:{account}:{quarter}" fields: account, quarter,
                  expected_amount, deposited, deposited_date
  setting         id="setting:{key}"            fields: key, value, updated_ts
  counter         id="counter:{table}"          fields: value (monotonic int)

Counters use ETag/If-Match concurrency to safely allocate sequential int IDs
shared between web requests and the background advisor worker.

The methods exposed here mirror :class:`advisor.state.SqliteStore` 1:1 so
callers can swap backends transparently via the ``Store()`` factory.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from azure.cosmos import CosmosClient, PartitionKey, exceptions
from azure.core import MatchConditions

from advisor.state import (
    DEFAULT_ACCOUNT,
    LEGACY_ACCOUNT_SEED,
    RunRecord,
    _slugify,
)


# Counter / lookup table name buckets
_COUNTER_USERS = "users"
_COUNTER_USER_ACCOUNTS = "user_accounts"
_COUNTER_RUNS = "runs"
_COUNTER_JOBS = "jobs"
_COUNTER_CONTRIBUTIONS = "contributions"

# Document type values (also partition keys)
_DT_USER = "user"
_DT_SESSION = "session"
_DT_USER_ACCOUNT = "user_account"
_DT_AI_USAGE = "ai_usage"
_DT_RUN = "run"
_DT_JOB = "job"
_DT_CONTRIBUTION = "contribution"
_DT_SETTING = "setting"
_DT_COUNTER = "counter"


class CosmosStore:
    """Cosmos DB backend for the portfolio advisor.

    Mirrors :class:`advisor.state.SqliteStore` so the rest of the app is
    backend-agnostic. Construct via the :func:`advisor.state.Store` factory
    so the underlying ``CosmosClient`` is reused across requests.
    """

    # Container TTL is enabled at provisioning time so per-item ``ttl``
    # fields (sessions, ai_usage) take effect.

    def __init__(self,
                 connection_string: str,
                 database_name: str = "TwoHundredSMA",
                 container_name: str = "Documents") -> None:
        self._lock = threading.Lock()
        self._client = CosmosClient.from_connection_string(connection_string)
        self._database = self._client.create_database_if_not_exists(database_name)
        self._container = self._database.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path="/documentType"),
            default_ttl=-1,  # enable TTL; -1 = "items with explicit ttl only"
        )

    # ---------------------------------------------------------------- core
    def _read(self, item_id: str, pk: str) -> Optional[Dict[str, Any]]:
        try:
            return self._container.read_item(item=item_id, partition_key=pk)
        except exceptions.CosmosResourceNotFoundError:
            return None

    def _query(self, pk: Optional[str], sql: str,
               params: Optional[List[Dict[str, Any]]] = None,
               cross_partition: bool = False) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {"query": sql,
                                  "parameters": params or [],
                                  "enable_cross_partition_query": cross_partition}
        if pk is not None and not cross_partition:
            kwargs["partition_key"] = pk
        return list(self._container.query_items(**kwargs))

    def _delete(self, item_id: str, pk: str) -> bool:
        try:
            self._container.delete_item(item=item_id, partition_key=pk)
            return True
        except exceptions.CosmosResourceNotFoundError:
            return False

    def _next_seq(self, table: str) -> int:
        """Allocate a monotonically increasing int ID for ``table`` using
        optimistic concurrency on a single counter document."""
        cid = f"counter:{table}"
        for attempt in range(20):
            doc = self._read(cid, _DT_COUNTER)
            if doc is None:
                try:
                    self._container.create_item({
                        "id": cid,
                        "documentType": _DT_COUNTER,
                        "table": table,
                        "value": 1,
                    })
                    return 1
                except exceptions.CosmosResourceExistsError:
                    continue
            new_val = int(doc.get("value", 0)) + 1
            doc["value"] = new_val
            try:
                self._container.replace_item(
                    item=cid,
                    body=doc,
                    etag=doc["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return new_val
            except (exceptions.CosmosAccessConditionFailedError,
                    exceptions.CosmosHttpResponseError) as e:  # noqa: BLE001
                # 412 Precondition Failed -> retry; other 4xx may be transient
                if isinstance(e, exceptions.CosmosHttpResponseError) and getattr(e, "status_code", 0) not in (412, 449):
                    raise
                time.sleep(0.01 * (attempt + 1))
                continue
        raise RuntimeError(f"counter contention exhausted: {table}")

    # ---------------------------------------------------------- settings
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        doc = self._read(f"setting:{key}", _DT_SETTING)
        return doc["value"] if doc else default

    def set_setting(self, key: str, value: str) -> None:
        self._container.upsert_item({
            "id": f"setting:{key}",
            "documentType": _DT_SETTING,
            "key": key,
            "value": value,
            "updated_ts": datetime.utcnow().isoformat(),
        })

    def get_last_peak_spy(self) -> Optional[float]:
        v = self.get_setting("last_peak_spy")
        return float(v) if v else None

    def set_last_peak_spy(self, v: float) -> None:
        self.set_setting("last_peak_spy", str(v))

    # ------------------------------------------------- contributions
    def get_contribution(self, quarter: str,
                         account: str = DEFAULT_ACCOUNT) -> Optional[Dict[str, Any]]:
        doc = self._read(f"contrib:{account}:{quarter}", _DT_CONTRIBUTION)
        if not doc:
            return None
        return self._strip_meta(doc)

    def upsert_contribution(self, quarter: str, expected_amount: float = 1750.0,
                            account: str = DEFAULT_ACCOUNT) -> None:
        cid = f"contrib:{account}:{quarter}"
        existing = self._read(cid, _DT_CONTRIBUTION)
        if existing:
            return
        seq = self._next_seq(_COUNTER_CONTRIBUTIONS)
        try:
            self._container.create_item({
                "id": cid,
                "documentType": _DT_CONTRIBUTION,
                "seq": seq,
                "account": account,
                "quarter": quarter,
                "expected_amount": float(expected_amount),
                "deposited": 0,
                "deposited_date": None,
            })
        except exceptions.CosmosResourceExistsError:
            pass

    def mark_contribution_deposited(self, quarter: str,
                                    when: Optional[date] = None,
                                    account: str = DEFAULT_ACCOUNT) -> None:
        when = when or date.today()
        cid = f"contrib:{account}:{quarter}"
        doc = self._read(cid, _DT_CONTRIBUTION)
        if not doc:
            return
        doc["deposited"] = 1
        doc["deposited_date"] = when.isoformat()
        self._container.replace_item(item=cid, body=doc)

    def list_contributions(self, account: str = DEFAULT_ACCOUNT) -> List[Dict[str, Any]]:
        rows = self._query(
            _DT_CONTRIBUTION,
            "SELECT * FROM c WHERE c.documentType=@dt AND c.account=@a "
            "ORDER BY c.quarter DESC",
            [{"name": "@dt", "value": _DT_CONTRIBUTION},
             {"name": "@a", "value": account}],
        )
        return [self._strip_meta(r) for r in rows]

    # --------------------------------------------------------------- runs
    def insert_run(self, rec: RunRecord) -> int:
        seq = self._next_seq(_COUNTER_RUNS)
        doc = {
            "id": str(seq),
            "documentType": _DT_RUN,
            "seq": seq,
            "account": rec.account,
            "run_ts": rec.run_ts,
            "asof_date": rec.asof_date,
            "run_type": rec.run_type,
            "portfolio_value": rec.portfolio_value,
            "cash": rec.cash,
            "stocks_pct": rec.stocks_pct,
            "bnd_pct": rec.bnd_pct,
            "last_peak_spy": rec.last_peak_spy,
            "current_spy": rec.current_spy,
            "spy_drawdown": rec.spy_drawdown,
            "orders": rec.orders,
            "holdings_before": rec.holdings_before,
            "holdings_target": rec.holdings_target,
            "price_snapshot": rec.price_snapshot or {},
            "cost_basis": rec.cost_basis,
            "cost_basis_by_ticker": rec.cost_basis_by_ticker or {},
            "ai_status": rec.ai_status,
            "ai_review": rec.ai_review,
            "notes": rec.notes,
        }
        self._container.create_item(doc)
        return seq

    def get_run(self, run_id: int,
                account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        doc = self._read(str(run_id), _DT_RUN)
        if not doc:
            return None
        if account is not None and doc.get("account") != account:
            return None
        return self._run_doc_to_app(doc)

    def list_runs(self, limit: int = 100,
                  account: Optional[str] = None) -> List[Dict[str, Any]]:
        if account is None:
            rows = self._query(
                _DT_RUN,
                "SELECT TOP @lim * FROM c WHERE c.documentType=@dt "
                "ORDER BY c.run_ts DESC",
                [{"name": "@dt", "value": _DT_RUN},
                 {"name": "@lim", "value": int(limit)}],
            )
        else:
            rows = self._query(
                _DT_RUN,
                "SELECT TOP @lim * FROM c WHERE c.documentType=@dt "
                "AND c.account=@a ORDER BY c.run_ts DESC",
                [{"name": "@dt", "value": _DT_RUN},
                 {"name": "@a", "value": account},
                 {"name": "@lim", "value": int(limit)}],
            )
        return [self._run_doc_to_app(r) for r in rows]

    def last_run(self, account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        rows = self.list_runs(limit=1, account=account)
        return rows[0] if rows else None

    def delete_run(self, run_id: int,
                   account: Optional[str] = None) -> bool:
        if account is not None:
            doc = self._read(str(run_id), _DT_RUN)
            if not doc or doc.get("account") != account:
                return False
        return self._delete(str(run_id), _DT_RUN)

    # --------------------------------------------------------------- jobs
    def create_job(self, account: str, kind: str = "advisor",
                   step_total: int = 5, step_name: str = "Queued",
                   message: str = "Waiting for worker...") -> int:
        seq = self._next_seq(_COUNTER_JOBS)
        ts = datetime.utcnow().isoformat()
        self._container.create_item({
            "id": str(seq),
            "documentType": _DT_JOB,
            "seq": seq,
            "account": account,
            "kind": kind,
            "status": "queued",
            "step_idx": 0,
            "step_total": int(step_total),
            "step_name": step_name,
            "message": message,
            "progress_pct": 0.0,
            "started_at": ts,
            "finished_at": None,
            "run_id": None,
            "redirect_url": None,
            "error": None,
            "version": 0,
        })
        return seq

    def get_job(self, job_id: int,
                account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        doc = self._read(str(job_id), _DT_JOB)
        if not doc:
            return None
        if account is not None and doc.get("account") != account:
            return None
        return self._job_doc_to_app(doc)

    def list_active_jobs(self, account: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._query(
            _DT_JOB,
            "SELECT TOP @lim * FROM c WHERE c.documentType=@dt AND c.account=@a "
            "AND (c.status='queued' OR c.status='running') "
            "ORDER BY c.started_at DESC",
            [{"name": "@dt", "value": _DT_JOB},
             {"name": "@a", "value": account},
             {"name": "@lim", "value": int(limit)}],
        )
        return [self._job_doc_to_app(r) for r in rows]

    def list_recent_jobs(self, account: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._query(
            _DT_JOB,
            "SELECT TOP @lim * FROM c WHERE c.documentType=@dt AND c.account=@a "
            "ORDER BY c.started_at DESC",
            [{"name": "@dt", "value": _DT_JOB},
             {"name": "@a", "value": account},
             {"name": "@lim", "value": int(limit)}],
        )
        return [self._job_doc_to_app(r) for r in rows]

    def _patch_job(self, job_id: int, updates: Dict[str, Any]) -> None:
        """Read-modify-write on a job doc. ETag retry for write concurrency."""
        for attempt in range(8):
            doc = self._read(str(job_id), _DT_JOB)
            if not doc:
                return
            doc.update(updates)
            doc["version"] = int(doc.get("version", 0)) + 1
            try:
                self._container.replace_item(
                    item=str(job_id), body=doc,
                    etag=doc["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return
            except (exceptions.CosmosAccessConditionFailedError,
                    exceptions.CosmosHttpResponseError) as e:  # noqa: BLE001
                if isinstance(e, exceptions.CosmosHttpResponseError) and getattr(e, "status_code", 0) not in (412, 449):
                    raise
                time.sleep(0.01 * (attempt + 1))
        # Last-resort: drop the etag and overwrite.
        doc = self._read(str(job_id), _DT_JOB)
        if doc:
            doc.update(updates)
            doc["version"] = int(doc.get("version", 0)) + 1
            self._container.replace_item(item=str(job_id), body=doc)

    def update_job_progress(self, job_id: int, step_idx: int, step_total: int,
                            step_name: str, message: str,
                            progress_pct: Optional[float] = None,
                            status: Optional[str] = None) -> None:
        if progress_pct is None:
            denom = max(1, step_total)
            progress_pct = round(100.0 * step_idx / denom, 1)
        updates: Dict[str, Any] = {
            "step_idx": int(step_idx),
            "step_total": int(step_total),
            "step_name": step_name,
            "message": message,
            "progress_pct": float(progress_pct),
        }
        if status:
            updates["status"] = status
        self._patch_job(job_id, updates)

    def mark_job_running(self, job_id: int) -> None:
        self._patch_job(job_id, {
            "status": "running",
            "step_name": "Starting",
            "message": "Worker picked up the job",
        })

    def mark_job_done(self, job_id: int, run_id: int) -> None:
        ts = datetime.utcnow().isoformat()
        # Pull job to read step_total (needed for step_idx=step_total)
        doc = self._read(str(job_id), _DT_JOB)
        step_total = int(doc.get("step_total", 1)) if doc else 1
        self._patch_job(job_id, {
            "status": "done",
            "step_idx": step_total,
            "progress_pct": 100.0,
            "step_name": "Complete",
            "message": "Run finished",
            "finished_at": ts,
            "run_id": int(run_id),
        })

    def mark_job_failed(self, job_id: int, error: str) -> None:
        ts = datetime.utcnow().isoformat()
        self._patch_job(job_id, {
            "status": "failed",
            "step_name": "Failed",
            "message": (error or "")[:500],
            "finished_at": ts,
            "error": (error or "")[:2000],
        })

    def mark_job_pushback(self, job_id: int, reason: str, redirect_url: str) -> None:
        ts = datetime.utcnow().isoformat()
        self._patch_job(job_id, {
            "status": "pushback",
            "step_name": "Cadence warning",
            "message": (reason or "")[:500],
            "finished_at": ts,
            "redirect_url": redirect_url,
        })

    def cleanup_stale_jobs(self) -> int:
        """Mark any queued/running jobs as failed (interrupted by restart).

        Safe with ACA single-revision + min/max replicas = 1: when we boot a
        new replica the old one is gone, so any in-flight job is dead.
        """
        rows = self._query(
            _DT_JOB,
            "SELECT c.id FROM c WHERE c.documentType=@dt "
            "AND (c.status='queued' OR c.status='running')",
            [{"name": "@dt", "value": _DT_JOB}],
        )
        ts = datetime.utcnow().isoformat()
        n = 0
        for r in rows:
            try:
                self._patch_job(int(r["id"]), {
                    "status": "failed",
                    "step_name": "Interrupted",
                    "message": "Interrupted by app restart; please re-upload portfolio.",
                    "finished_at": ts,
                    "error": "app_restart",
                })
                n += 1
            except Exception:  # noqa: BLE001
                continue
        return n

    # ----------------------------------------------------------- users
    def create_user(self, username: str, password_hash: str,
                    user_type: str = "regular") -> int:
        u = (username or "").strip()
        if not u:
            raise ValueError("username required")
        lc = u.lower()
        ts = datetime.utcnow().isoformat()
        seq = self._next_seq(_COUNTER_USERS)
        try:
            self._container.create_item({
                "id": f"user_username:{lc}",
                "documentType": _DT_USER,
                "seq": seq,
                "username": u,
                "username_lc": lc,
                "password_hash": password_hash,
                "type": user_type,
                "created_at": ts,
            })
        except exceptions.CosmosResourceExistsError as e:
            raise ValueError("Username already in use") from e
        if user_type == "admin":
            self._seed_legacy_accounts_for_user(seq)
        return seq

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        u = (username or "").strip().lower()
        if not u:
            return None
        doc = self._read(f"user_username:{u}", _DT_USER)
        return self._user_doc_to_app(doc) if doc else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        rows = self._query(
            _DT_USER,
            "SELECT TOP 1 * FROM c WHERE c.documentType=@dt AND c.seq=@s",
            [{"name": "@dt", "value": _DT_USER},
             {"name": "@s", "value": int(user_id)}],
        )
        return self._user_doc_to_app(rows[0]) if rows else None

    def admin_exists(self) -> bool:
        rows = self._query(
            _DT_USER,
            "SELECT TOP 1 c.id FROM c WHERE c.documentType=@dt AND c.type='admin'",
            [{"name": "@dt", "value": _DT_USER}],
        )
        return bool(rows)

    # -------------------------------------------------------- sessions
    def create_session(self, user_id: int, token: str, ttl_days: int = 30) -> None:
        ts = datetime.utcnow()
        ttl_seconds = max(60, ttl_days * 86400) if ttl_days > 0 else 60
        self._container.create_item({
            "id": token,
            "documentType": _DT_SESSION,
            "user_id": int(user_id),
            "created_at": ts.isoformat(),
            "expires_at": (ts + timedelta(seconds=ttl_seconds)).isoformat(),
            "ttl": ttl_seconds,
        })

    def get_session_user(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        sess = self._read(token, _DT_SESSION)
        if not sess:
            return None
        try:
            if datetime.fromisoformat(sess["expires_at"]) <= datetime.utcnow():
                return None
        except (KeyError, ValueError):
            return None
        return self.get_user_by_id(int(sess["user_id"]))

    def delete_session(self, token: str) -> None:
        if not token:
            return
        self._delete(token, _DT_SESSION)

    def cleanup_expired_sessions(self) -> int:
        # Cosmos TTL handles this for free; nothing to do.
        return 0

    # ------------------------------------------------- user_accounts
    def _seed_legacy_accounts_for_user(self, user_id: int) -> None:
        ts = datetime.utcnow().isoformat()
        for slug, label in LEGACY_ACCOUNT_SEED:
            cid = f"useracct:{user_id}:{slug}"
            if self._read(cid, _DT_USER_ACCOUNT):
                continue
            seq = self._next_seq(_COUNTER_USER_ACCOUNTS)
            try:
                self._container.create_item({
                    "id": cid,
                    "documentType": _DT_USER_ACCOUNT,
                    "seq": seq,
                    "user_id": int(user_id),
                    "slug": slug,
                    "label": label,
                    "created_at": ts,
                })
            except exceptions.CosmosResourceExistsError:
                continue

    def list_user_accounts(self, user_id: int) -> List[Dict[str, Any]]:
        rows = self._query(
            _DT_USER_ACCOUNT,
            "SELECT * FROM c WHERE c.documentType=@dt AND c.user_id=@u "
            "ORDER BY c.seq ASC",
            [{"name": "@dt", "value": _DT_USER_ACCOUNT},
             {"name": "@u", "value": int(user_id)}],
        )
        return [self._user_account_to_app(r) for r in rows]

    def create_user_account(self, user_id: int, label: str,
                            slug: Optional[str] = None) -> Dict[str, Any]:
        label = (label or "").strip()
        if not label:
            raise ValueError("Account label is required")
        base = _slugify(slug or label)
        slug_base = f"u{user_id}_{base}"
        ts = datetime.utcnow().isoformat()
        for n in range(1, 100):
            candidate = slug_base if n == 1 else f"{slug_base}_{n}"
            cid = f"useracct:{user_id}:{candidate}"
            if self._read(cid, _DT_USER_ACCOUNT):
                continue
            seq = self._next_seq(_COUNTER_USER_ACCOUNTS)
            try:
                self._container.create_item({
                    "id": cid,
                    "documentType": _DT_USER_ACCOUNT,
                    "seq": seq,
                    "user_id": int(user_id),
                    "slug": candidate,
                    "label": label,
                    "created_at": ts,
                })
                return {
                    "id": seq,
                    "user_id": int(user_id),
                    "slug": candidate,
                    "label": label,
                    "created_at": ts,
                }
            except exceptions.CosmosResourceExistsError:
                continue
        raise RuntimeError("Could not allocate a unique slug for account")

    def get_user_account_by_slug(self, user_id: int,
                                 slug: str) -> Optional[Dict[str, Any]]:
        doc = self._read(f"useracct:{user_id}:{slug}", _DT_USER_ACCOUNT)
        return self._user_account_to_app(doc) if doc else None

    def delete_user_account(self, user_id: int, account_id: int) -> bool:
        rows = self._query(
            _DT_USER_ACCOUNT,
            "SELECT TOP 1 c.id FROM c WHERE c.documentType=@dt "
            "AND c.user_id=@u AND c.seq=@s",
            [{"name": "@dt", "value": _DT_USER_ACCOUNT},
             {"name": "@u", "value": int(user_id)},
             {"name": "@s", "value": int(account_id)}],
        )
        if not rows:
            return False
        return self._delete(rows[0]["id"], _DT_USER_ACCOUNT)

    def user_owns_account_slug(self, user_id: int, slug: str) -> bool:
        return self.get_user_account_by_slug(user_id, slug) is not None

    # ----------------------------------------------------- AI usage
    def ai_used_today(self, user_id: int, today: Optional[date] = None) -> bool:
        d = (today or date.today()).isoformat()
        doc = self._read(f"aiu:{user_id}:{d}", _DT_AI_USAGE)
        return doc is not None

    def ai_usage_count_today(self, user_id: int,
                             today: Optional[date] = None) -> int:
        return 1 if self.ai_used_today(user_id, today) else 0

    def log_ai_usage(self, user_id: int, run_id: Optional[int] = None,
                     when: Optional[date] = None) -> None:
        d = (when or date.today()).isoformat()
        cid = f"aiu:{user_id}:{d}"
        ttl_seconds = 90 * 86400  # keep usage rows for 90d for auditing
        try:
            self._container.create_item({
                "id": cid,
                "documentType": _DT_AI_USAGE,
                "user_id": int(user_id),
                "used_date": d,
                "run_id": int(run_id) if run_id is not None else None,
                "created_at": datetime.utcnow().isoformat(),
                "ttl": ttl_seconds,
            })
        except exceptions.CosmosResourceExistsError:
            # Already logged today; quota check is idempotent.
            return

    # ============================================================
    # Shape helpers
    # ============================================================
    @staticmethod
    def _strip_meta(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in doc.items() if not k.startswith("_")}

    @staticmethod
    def _user_doc_to_app(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not doc:
            return None
        return {
            "id": int(doc["seq"]),
            "username": doc.get("username"),
            "password_hash": doc.get("password_hash"),
            "type": doc.get("type", "regular"),
            "created_at": doc.get("created_at"),
        }

    @staticmethod
    def _user_account_to_app(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not doc:
            return None
        return {
            "id": int(doc["seq"]),
            "user_id": int(doc["user_id"]),
            "slug": doc.get("slug"),
            "label": doc.get("label"),
            "created_at": doc.get("created_at"),
        }

    @staticmethod
    def _job_doc_to_app(doc: Dict[str, Any]) -> Dict[str, Any]:
        out = CosmosStore._strip_meta(doc)
        out["id"] = int(doc.get("seq", doc["id"]))
        for k in ("step_idx", "step_total", "version", "run_id"):
            if out.get(k) is not None:
                try:
                    out[k] = int(out[k])
                except (TypeError, ValueError):
                    pass
        if out.get("progress_pct") is not None:
            out["progress_pct"] = float(out["progress_pct"])
        return out

    @staticmethod
    def _run_doc_to_app(doc: Dict[str, Any]) -> Dict[str, Any]:
        out = CosmosStore._strip_meta(doc)
        out["id"] = int(doc.get("seq", doc["id"]))
        # SQLite-backed code paths sometimes look at *_json sibling keys; populate
        # them defensively so calling code that accesses either form keeps working.
        import json as _json
        for parsed_key, json_key in (
            ("orders", "orders_json"),
            ("holdings_before", "holdings_before_json"),
            ("holdings_target", "holdings_target_json"),
            ("price_snapshot", "price_snapshot_json"),
            ("cost_basis_by_ticker", "cost_basis_by_ticker_json"),
            ("ai_review", "ai_review_json"),
        ):
            val = out.get(parsed_key)
            if val is None:
                out.setdefault(json_key, None)
            else:
                try:
                    out[json_key] = _json.dumps(val)
                except (TypeError, ValueError):
                    out[json_key] = None
        return out
