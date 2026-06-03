"""Authentication helpers: password hashing, session cookies, FastAPI deps.

Passwords are hashed with hashlib.scrypt (stdlib, no extra deps). Sessions are
opaque random tokens stored in the `sessions` table, set on the client via an
HttpOnly cookie. Cookie lifetime mirrors session expiry (default 30 days).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any, Dict, Optional

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse

from advisor.state import Store


SESSION_COOKIE = "session"
SESSION_TTL_DAYS = 30

# scrypt parameters (RFC 7914 recommended for interactive logins; ~64ms on modern HW)
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_HASH_LEN = 32
_SALT_LEN = 16


def hash_password(password: str) -> str:
    """Return 'scrypt$N$r$p$salt_hex$hash_hex' for storage."""
    if not isinstance(password, str) or len(password) == 0:
        raise ValueError("Password must be a non-empty string")
    salt = secrets.token_bytes(_SALT_LEN)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=64 * 1024 * 1024,
        dklen=_HASH_LEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
    except (AttributeError, ValueError):
        return False
    if scheme != "scrypt":
        return False
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n, r=r, p=p,
        maxmem=64 * 1024 * 1024,
        dklen=len(expected),
    )
    return hmac.compare_digest(dk, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def admin_username() -> str:
    return (os.environ.get("ADMIN_USERNAME") or "andrewdikih").strip()


def is_admin_username(username: str) -> bool:
    return (username or "").strip().lower() == admin_username().lower()


# ---- FastAPI dependencies ----

def optional_user(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return Store().get_session_user(token)


def require_user(request: Request) -> Dict[str, Any]:
    user = optional_user(request)
    if user is None:
        # 303 redirect via HTTPException — caller (app exception handler) converts.
        raise HTTPException(
            status_code=307,
            detail="login_required",
            headers={"Location": "/login"},
        )
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    if (user.get("type") or "regular") != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user
