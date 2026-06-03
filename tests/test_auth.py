"""Unit tests for advisor.auth password hashing + helper functions."""
from __future__ import annotations

import pytest

from advisor.auth import (
    admin_username,
    hash_password,
    is_admin_username,
    new_session_token,
    verify_password,
)


class TestPasswordHashing:
    def test_roundtrip_succeeds_for_valid_password(self):
        h = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", h) is True

    def test_wrong_password_is_rejected(self):
        h = hash_password("hunter2")
        assert verify_password("hunter3", h) is False

    def test_hash_format_is_scrypt_with_n_r_p_salt_hash(self):
        h = hash_password("anything")
        parts = h.split("$")
        assert len(parts) == 6
        scheme, n, r, p, salt_hex, hash_hex = parts
        assert scheme == "scrypt"
        assert int(n) >= 2 ** 14
        assert int(r) >= 1
        assert int(p) >= 1
        # salt + hash are hex-encoded.
        bytes.fromhex(salt_hex)
        bytes.fromhex(hash_hex)

    def test_same_password_produces_different_hash_each_call(self):
        # Different salt per call -> different stored string.
        assert hash_password("same") != hash_password("same")

    def test_empty_password_is_rejected(self):
        with pytest.raises(ValueError):
            hash_password("")

    def test_non_string_password_is_rejected(self):
        with pytest.raises(ValueError):
            hash_password(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_hash", [
        "",                       # empty
        "not-a-hash",             # wrong format
        "scrypt$1$1$1$xx$yy",     # invalid hex
        "bcrypt$1$1$1$aa$bb",     # wrong scheme
        "scrypt$1$1$1",           # too few fields
    ])
    def test_verify_returns_false_for_malformed_hash(self, bad_hash):
        assert verify_password("anything", bad_hash) is False


class TestSessionToken:
    def test_token_is_urlsafe_and_non_empty(self):
        t = new_session_token()
        assert t and len(t) >= 32
        # URL-safe base64 alphabet only.
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert set(t).issubset(allowed)

    def test_tokens_are_unique(self):
        n = 100
        assert len({new_session_token() for _ in range(n)}) == n


class TestAdminUsername:
    def test_defaults_to_andrewdikih(self, monkeypatch):
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        assert admin_username() == "andrewdikih"

    def test_respects_env_var(self, monkeypatch):
        monkeypatch.setenv("ADMIN_USERNAME", "alice")
        assert admin_username() == "alice"

    def test_is_admin_match_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ADMIN_USERNAME", "Alice")
        assert is_admin_username("ALICE") is True
        assert is_admin_username("alice") is True
        assert is_admin_username("bob") is False
