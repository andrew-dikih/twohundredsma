"""Unit tests for the pure-function helpers in advisor.store_cosmos.

These exercise the document<->app shape mappers without requiring a live
Cosmos endpoint. The CosmosClient itself is integration-tested via the
``COSMOS_CONNECTION_STRING`` smoke path in deploy environments.
"""
from __future__ import annotations

import json

from advisor.store_cosmos import CosmosStore


class TestStripMeta:
    def test_drops_underscore_prefixed_fields(self):
        doc = {"id": "1", "value": 42, "_etag": "abc", "_rid": "xyz", "_ts": 1}
        out = CosmosStore._strip_meta(doc)
        assert out == {"id": "1", "value": 42}


class TestUserDocMapping:
    def test_seq_becomes_id_as_int(self):
        doc = {
            "id": "user_username:alice", "documentType": "user",
            "seq": 42, "username": "alice", "username_lc": "alice",
            "password_hash": "scrypt$...", "type": "regular",
            "created_at": "2026-06-01T00:00:00",
            "_etag": "abc",
        }
        out = CosmosStore._user_doc_to_app(doc)
        assert out["id"] == 42
        assert isinstance(out["id"], int)
        assert out["username"] == "alice"
        assert out["password_hash"] == "scrypt$..."
        assert out["type"] == "regular"
        assert "_etag" not in out  # internal-only

    def test_none_passthrough(self):
        assert CosmosStore._user_doc_to_app(None) is None


class TestUserAccountMapping:
    def test_shape_matches_sqlite(self):
        doc = {
            "id": "useracct:1:u1_brokerage", "documentType": "user_account",
            "seq": 7, "user_id": 1, "slug": "u1_brokerage",
            "label": "Brokerage", "created_at": "2026-06-01T00:00:00",
            "_etag": "abc",
        }
        out = CosmosStore._user_account_to_app(doc)
        assert out == {
            "id": 7, "user_id": 1, "slug": "u1_brokerage",
            "label": "Brokerage", "created_at": "2026-06-01T00:00:00",
        }


class TestJobDocMapping:
    def test_seq_becomes_id_and_numerics_are_typed(self):
        doc = {
            "id": "12", "documentType": "job", "seq": 12,
            "account": "a", "kind": "advisor", "status": "running",
            "step_idx": "2", "step_total": "5", "step_name": "halfway",
            "message": "working", "progress_pct": "40",
            "started_at": "2026-06-01T00:00:00", "finished_at": None,
            "run_id": "99", "redirect_url": None, "error": None,
            "version": "3", "_etag": "abc",
        }
        out = CosmosStore._job_doc_to_app(doc)
        assert out["id"] == 12
        assert out["step_idx"] == 2
        assert out["step_total"] == 5
        assert out["run_id"] == 99
        assert out["version"] == 3
        assert out["progress_pct"] == 40.0
        assert "_etag" not in out


class TestRunDocMapping:
    def test_populates_both_parsed_and_json_keys(self):
        doc = {
            "id": "5", "documentType": "run", "seq": 5,
            "account": "a", "run_ts": "2026-06-01T12:00:00",
            "asof_date": "2026-06-01", "run_type": "rebalance",
            "portfolio_value": 10000.0, "cash": 1000.0,
            "orders": [{"ticker": "SPY", "shares": 1}],
            "holdings_before": {"SPY": 10},
            "holdings_target": {"SPY": 11},
            "price_snapshot": {"SPY": 480.0},
            "cost_basis": 9000.0,
            "cost_basis_by_ticker": {"SPY": 4500.0},
            "ai_review": {"summary": "ok"},
            "_etag": "abc",
        }
        out = CosmosStore._run_doc_to_app(doc)
        assert out["id"] == 5
        # Parsed forms preserved
        assert out["orders"] == [{"ticker": "SPY", "shares": 1}]
        assert out["holdings_before"] == {"SPY": 10}
        assert out["ai_review"] == {"summary": "ok"}
        # JSON-string siblings populated for templates / legacy callers
        assert json.loads(out["orders_json"])[0]["ticker"] == "SPY"
        assert json.loads(out["holdings_before_json"]) == {"SPY": 10}
        assert json.loads(out["ai_review_json"]) == {"summary": "ok"}

    def test_missing_optional_fields_become_none_json_keys(self):
        doc = {
            "id": "5", "documentType": "run", "seq": 5,
            "account": "a", "run_ts": "x", "asof_date": "y",
            "run_type": "hold", "portfolio_value": 0.0, "cash": 0.0,
            "orders": [], "holdings_before": {}, "holdings_target": {},
            # No price_snapshot / cost_basis_by_ticker / ai_review
        }
        out = CosmosStore._run_doc_to_app(doc)
        assert out["price_snapshot_json"] is None
        assert out["cost_basis_by_ticker_json"] is None
        assert out["ai_review_json"] is None
