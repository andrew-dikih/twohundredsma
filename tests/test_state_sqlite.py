"""Unit tests for the Store factory and the SQLite backend.

These cover the contract that ``advisor.store_cosmos.CosmosStore`` is expected
to honour as well, so any future divergence between the two backends shows up
as a failing test the next time the suite runs against Cosmos.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from advisor.auth import hash_password
from advisor.state import (
    DEFAULT_ACCOUNT,
    LEGACY_ACCOUNT_SEED,
    RunRecord,
    SqliteStore,
    Store,
    quarter_of,
)


def _make_run(account: str = DEFAULT_ACCOUNT,
              run_ts: str = "2026-06-01T12:00:00",
              run_type: str = "rebalance") -> RunRecord:
    return RunRecord(
        id=None,
        run_ts=run_ts,
        asof_date="2026-06-01",
        run_type=run_type,
        portfolio_value=10000.0,
        cash=1000.0,
        stocks_pct=0.75,
        bnd_pct=0.25,
        last_peak_spy=500.0,
        current_spy=480.0,
        spy_drawdown=-0.04,
        orders=[{"ticker": "SPY", "shares": 1, "side": "buy"}],
        holdings_before={"SPY": 10, "BND": 5},
        holdings_target={"SPY": 11, "BND": 5},
        price_snapshot={"SPY": 480.0, "BND": 73.0},
        cost_basis=9500.0,
        cost_basis_by_ticker={"SPY": 4500.0, "BND": 365.0},
        ai_status="full",
        ai_review={"summary": "looks good"},
        notes="test",
        account=account,
    )


class TestStoreFactory:
    def test_returns_sqlite_when_no_cosmos_env(self, monkeypatch):
        monkeypatch.delenv("COSMOS_CONNECTION_STRING", raising=False)
        from advisor import state as state_mod
        state_mod._reset_store_singleton_for_tests()
        s = Store()
        assert isinstance(s, SqliteStore)

    def test_caches_singleton(self):
        assert Store() is Store()

    def test_explicit_db_path_bypasses_singleton(self, tmp_path):
        # When an explicit path is passed, the factory returns a fresh
        # SqliteStore against that DB regardless of the cached backend.
        path = tmp_path / "alt.db"
        s = Store(db_path=str(path))
        assert isinstance(s, SqliteStore)
        assert s.db_path == str(path)


class TestUsersAndSessions:
    def test_create_admin_seeds_legacy_accounts(self):
        store = Store()
        uid = store.create_user("admin1", hash_password("pw12345678"), user_type="admin")
        accounts = store.list_user_accounts(uid)
        slugs = {a["slug"] for a in accounts}
        for slug, _ in LEGACY_ACCOUNT_SEED:
            assert slug in slugs

    def test_regular_user_has_no_default_accounts(self):
        store = Store()
        uid = store.create_user("alice", hash_password("pw12345678"))
        assert store.list_user_accounts(uid) == []

    def test_admin_exists_flips_when_first_admin_is_created(self):
        store = Store()
        assert store.admin_exists() is False
        store.create_user("alice", hash_password("pw12345678"), user_type="regular")
        assert store.admin_exists() is False
        store.create_user("root", hash_password("pw12345678"), user_type="admin")
        assert store.admin_exists() is True

    def test_username_lookup_is_case_insensitive(self):
        store = Store()
        store.create_user("AlIcE", hash_password("pw12345678"))
        assert store.get_user_by_username("alice")["username"].lower() == "alice"
        assert store.get_user_by_username("ALICE") is not None

    def test_get_user_by_id_round_trip(self):
        store = Store()
        uid = store.create_user("bob", hash_password("pw12345678"))
        u = store.get_user_by_id(uid)
        assert u and u["id"] == uid and u["username"] == "bob"

    def test_session_lifecycle(self):
        store = Store()
        uid = store.create_user("eve", hash_password("pw12345678"))
        store.create_session(uid, "token-eve")
        u = store.get_session_user("token-eve")
        assert u and u["id"] == uid
        store.delete_session("token-eve")
        assert store.get_session_user("token-eve") is None

    def test_get_session_user_returns_none_for_unknown_token(self):
        assert Store().get_session_user("does-not-exist") is None


class TestUserAccounts:
    def test_create_and_list(self):
        store = Store()
        uid = store.create_user("alice", hash_password("pw12345678"))
        a = store.create_user_account(uid, "My Brokerage")
        assert a["label"] == "My Brokerage"
        assert a["slug"].startswith(f"u{uid}_")
        assert store.list_user_accounts(uid)[0]["id"] == a["id"]

    def test_duplicate_label_gets_unique_slug(self):
        store = Store()
        uid = store.create_user("alice", hash_password("pw12345678"))
        a1 = store.create_user_account(uid, "Roth IRA")
        a2 = store.create_user_account(uid, "Roth IRA")
        assert a1["slug"] != a2["slug"]

    def test_blank_label_rejected(self):
        store = Store()
        uid = store.create_user("alice", hash_password("pw12345678"))
        with pytest.raises(ValueError):
            store.create_user_account(uid, "")

    def test_user_owns_account_slug(self):
        store = Store()
        uid = store.create_user("alice", hash_password("pw12345678"))
        a = store.create_user_account(uid, "Brokerage")
        assert store.user_owns_account_slug(uid, a["slug"]) is True
        assert store.user_owns_account_slug(uid, "not-mine") is False

    def test_delete_user_account_removes_it(self):
        store = Store()
        uid = store.create_user("alice", hash_password("pw12345678"))
        a = store.create_user_account(uid, "Brokerage")
        assert store.delete_user_account(uid, a["id"]) is True
        assert store.list_user_accounts(uid) == []


class TestRuns:
    def test_insert_and_get(self):
        store = Store()
        rid = store.insert_run(_make_run())
        assert isinstance(rid, int) and rid > 0
        r = store.get_run(rid)
        assert r and r["run_type"] == "rebalance"
        # JSON columns are auto-parsed.
        assert r["orders"][0]["ticker"] == "SPY"
        assert r["holdings_before"]["SPY"] == 10

    def test_account_scoping(self):
        store = Store()
        rid_a = store.insert_run(_make_run(account="acct_a"))
        rid_b = store.insert_run(_make_run(account="acct_b",
                                           run_ts="2026-06-02T12:00:00"))
        assert store.get_run(rid_a, account="acct_b") is None
        assert store.get_run(rid_a, account="acct_a")["id"] == rid_a
        assert [r["id"] for r in store.list_runs(account="acct_a")] == [rid_a]

    def test_last_run_returns_most_recent(self):
        store = Store()
        store.insert_run(_make_run(run_ts="2026-06-01T12:00:00"))
        rid_recent = store.insert_run(_make_run(run_ts="2026-06-02T12:00:00"))
        assert store.last_run()["id"] == rid_recent

    def test_delete_run_is_scoped(self):
        store = Store()
        rid = store.insert_run(_make_run(account="acct_a"))
        assert store.delete_run(rid, account="acct_b") is False
        assert store.delete_run(rid, account="acct_a") is True
        assert store.get_run(rid) is None


class TestJobs:
    def test_progress_lifecycle(self):
        store = Store()
        jid = store.create_job(account=DEFAULT_ACCOUNT, kind="advisor",
                               step_total=4, step_name="queued")
        store.mark_job_running(jid)
        store.update_job_progress(jid, step_idx=2, step_total=4,
                                  step_name="halfway",
                                  message="working", status="running")
        rid = store.insert_run(_make_run())
        store.mark_job_done(jid, rid)
        j = store.get_job(jid)
        assert j["status"] == "done"
        assert j["progress_pct"] == 100.0
        assert j["run_id"] == rid
        assert j["version"] >= 3  # at least three updates incremented it

    def test_cleanup_stale_jobs_marks_in_flight_as_failed(self):
        store = Store()
        jid = store.create_job(account=DEFAULT_ACCOUNT)
        store.mark_job_running(jid)
        n = store.cleanup_stale_jobs()
        assert n == 1
        assert store.get_job(jid)["status"] == "failed"

    def test_list_active_excludes_finished_jobs(self):
        store = Store()
        j_done = store.create_job(account=DEFAULT_ACCOUNT)
        store.mark_job_running(j_done)
        rid = store.insert_run(_make_run())
        store.mark_job_done(j_done, rid)
        j_running = store.create_job(account=DEFAULT_ACCOUNT)
        store.mark_job_running(j_running)
        active = store.list_active_jobs(account=DEFAULT_ACCOUNT)
        ids = {j["id"] for j in active}
        assert j_running in ids
        assert j_done not in ids


class TestContributions:
    def test_upsert_and_mark_deposited(self):
        store = Store()
        store.upsert_contribution("2026-Q2")
        c = store.get_contribution("2026-Q2")
        assert c and c["deposited"] == 0
        store.mark_contribution_deposited("2026-Q2", when=date(2026, 4, 15))
        c = store.get_contribution("2026-Q2")
        assert c["deposited"] == 1
        assert c["deposited_date"] == "2026-04-15"

    def test_account_scoped_contributions_are_independent(self):
        store = Store()
        store.upsert_contribution("2026-Q2", account="acct_a")
        store.upsert_contribution("2026-Q2", account="acct_b")
        assert len(store.list_contributions(account="acct_a")) == 1
        assert len(store.list_contributions(account="acct_b")) == 1

    def test_quarter_of_returns_yyyy_qn(self):
        assert quarter_of(date(2026, 1, 15)) == "2026-Q1"
        assert quarter_of(date(2026, 4, 1)) == "2026-Q2"
        assert quarter_of(date(2026, 12, 31)) == "2026-Q4"


class TestSettings:
    def test_get_set_roundtrip(self):
        store = Store()
        assert store.get_setting("foo") is None
        assert store.get_setting("foo", default="bar") == "bar"
        store.set_setting("foo", "baz")
        assert store.get_setting("foo") == "baz"

    def test_set_setting_is_idempotent_upsert(self):
        store = Store()
        store.set_setting("foo", "1")
        store.set_setting("foo", "2")
        assert store.get_setting("foo") == "2"

    def test_last_peak_spy_helpers(self):
        store = Store()
        store.set_last_peak_spy(642.5)
        assert store.get_last_peak_spy() == 642.5


class TestAiQuota:
    def test_used_today_flips_after_log(self):
        store = Store()
        uid = store.create_user("alice", hash_password("pw12345678"))
        assert store.ai_used_today(uid) is False
        store.log_ai_usage(uid, run_id=None)
        assert store.ai_used_today(uid) is True

    def test_per_user_isolation(self):
        store = Store()
        a = store.create_user("alice", hash_password("pw12345678"))
        b = store.create_user("bob", hash_password("pw12345678"))
        store.log_ai_usage(a)
        assert store.ai_used_today(a) is True
        assert store.ai_used_today(b) is False
