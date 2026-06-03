"""Integration tests against the FastAPI app using the in-process TestClient.

These exercise the wire contract end-to-end: routes, templates, dependency
injection, authentication redirects, and session cookies. They run against an
isolated temporary SQLite DB per test (see ``tests/conftest.py``).
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from advisor.app import app


@pytest.fixture()
def client():
    # Disable follow-redirects so we can assert on Location headers directly.
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _register(client: TestClient, username: str = "alice",
              password: str = "pw12345678") -> None:
    r = client.post("/register",
                    data={"username": username,
                          "password": password,
                          "password2": password})
    assert r.status_code in (200, 302, 303), r.text


def _login(client: TestClient, username: str = "alice",
           password: str = "pw12345678") -> None:
    r = client.post("/login",
                    data={"username": username, "password": password})
    assert r.status_code in (302, 303), r.text
    # FastAPI/Starlette TestClient persists cookies across requests on this
    # client instance, so the session cookie is automatically attached to
    # subsequent calls.


class TestAuthRedirects:
    def test_anonymous_get_root_redirects_to_login(self, client):
        r = client.get("/")
        assert r.status_code in (302, 303, 307)
        assert "/login" in (r.headers.get("location") or "")

    def test_login_page_is_public(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "Log in" in r.text or "Sign in" in r.text.lower() or "login" in r.text.lower()

    def test_register_page_is_public(self, client):
        r = client.get("/register")
        assert r.status_code == 200
        assert "Create" in r.text or "Register" in r.text or "Sign up" in r.text.lower()


class TestRegistrationAndLogin:
    def test_register_creates_user_and_session_cookie(self, client):
        r = client.post("/register",
                        data={"username": "alice",
                              "password": "pw12345678",
                              "password2": "pw12345678"})
        assert r.status_code in (302, 303)
        # Session cookie set on success
        cookies = client.cookies
        assert "session" in cookies, f"no session cookie; got {dict(cookies)}"

    def test_register_rejects_password_mismatch(self, client):
        r = client.post("/register",
                        data={"username": "alice",
                              "password": "pw12345678",
                              "password2": "different1"})
        assert r.status_code in (302, 303)
        # Should redirect back to /register with an error param
        assert "/register" in r.headers["location"]
        assert "error=" in r.headers["location"]

    def test_register_rejects_short_password(self, client):
        r = client.post("/register",
                        data={"username": "alice",
                              "password": "short",
                              "password2": "short"})
        assert r.status_code in (302, 303)
        assert "error=" in r.headers["location"]

    def test_first_user_with_admin_username_becomes_admin(self, client,
                                                           monkeypatch):
        monkeypatch.setenv("ADMIN_USERNAME", "root")
        _register(client, username="root", password="pw12345678")
        from advisor.state import Store
        u = Store().get_user_by_username("root")
        assert u is not None
        assert u["type"] == "admin"

    def test_login_with_wrong_password_redirects_with_error(self, client):
        _register(client)
        # Drop the session cookie so we get redirected via /login flow
        client.cookies.clear()
        r = client.post("/login",
                        data={"username": "alice", "password": "wrong-pw-12"})
        assert r.status_code in (302, 303)
        assert "error=" in r.headers["location"]


class TestAuthenticatedAccess:
    def test_home_renders_when_logged_in(self, client):
        _register(client)
        r = client.get("/")
        # Either renders home (200) or redirects to upload — both are valid.
        assert r.status_code in (200, 302, 303, 307)
        if r.status_code == 200:
            # Layout should include the user's nav element when authenticated.
            assert "alice" in r.text.lower() or "logout" in r.text.lower()

    def test_logout_clears_session_cookie(self, client):
        _register(client)
        assert "session" in client.cookies
        r = client.post("/logout")
        assert r.status_code in (200, 302, 303)
        # After logout, the next /  request should redirect to /login.
        r2 = client.get("/")
        assert r2.status_code in (302, 303, 307)
        assert "/login" in (r2.headers.get("location") or "")

    def test_accounts_page_requires_auth(self, client):
        r = client.get("/accounts")
        assert r.status_code in (302, 303, 307)
        assert "/login" in (r.headers.get("location") or "")

    def test_accounts_page_renders_for_logged_in_user(self, client):
        _register(client)
        r = client.get("/accounts")
        assert r.status_code == 200
