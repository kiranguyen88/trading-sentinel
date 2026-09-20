"""Accounts, sessions, and the write-only chat log.

Email + password kept in our own `users` table rather than Supabase Auth, so the
browser never needs an anon key and the server has no JWTs to verify. Passwords
are hashed with werkzeug (ships with Flask — no new dependency).

All database access reuses trading_bot's PostgREST helpers and the service key,
matching how portfolio data is stored. RLS is on with no policies, so only the
service key reaches these tables.
"""

import os
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from trading_bot import (
    _SUPABASE_OK, _SUPABASE_URL, _sb_headers, _sb_load, _sb_save_user,
    set_current_user,
)

ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "").lower() in ("1", "true", "yes")


def _require_db():
    if not _SUPABASE_OK:
        raise RuntimeError(
            "Supabase is not configured (set SUPABASE_SERVICE_KEY) — accounts "
            "cannot be read or written."
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_USER_COLS = "id,email,password_hash,display_name,is_owner"


def get_user_by_email(email: str) -> dict | None:
    _require_db()
    r = requests.get(
        f"{_SUPABASE_URL}/rest/v1/users",
        params={"email": f"eq.{email.strip().lower()}", "select": _USER_COLS},
        headers=_sb_headers(), timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def get_user_by_id(uid: str) -> dict | None:
    _require_db()
    r = requests.get(
        f"{_SUPABASE_URL}/rest/v1/users",
        params={"id": f"eq.{uid}", "select": _USER_COLS},
        headers=_sb_headers(), timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def get_owner() -> dict | None:
    """The account that scheduled alerts and digests run for."""
    _require_db()
    r = requests.get(
        f"{_SUPABASE_URL}/rest/v1/users",
        params={"is_owner": "eq.true", "select": _USER_COLS,
                "order": "created_at.asc", "limit": "1"},
        headers=_sb_headers(), timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def create_user(email: str, password: str, display_name: str = "",
                is_owner: bool = False) -> dict:
    """Insert a user plus their empty portfolio row. Raises on duplicate email
    (the unique constraint on users.email is what enforces it)."""
    _require_db()
    email = email.strip().lower()
    r = requests.post(
        f"{_SUPABASE_URL}/rest/v1/users",
        headers={**_sb_headers(), "prefer": "return=representation"},
        json=[{
            "email": email,
            "password_hash": generate_password_hash(password),
            "display_name": display_name or email.split("@")[0],
            "is_owner": is_owner,
        }],
        timeout=15,
    )
    r.raise_for_status()
    user = r.json()[0]

    # Give them an empty portfolio so the dashboard renders instead of 500ing.
    _sb_save_user(user["id"], {"holdings": [], "watchlist": []})
    return user


def verify_login(email: str, password: str) -> dict | None:
    user = get_user_by_email(email)
    if not user or not check_password_hash(user["password_hash"], password):
        return None
    return user


MIN_PASSWORD_LEN = 8


def change_password(uid: str, current_password: str, new_password: str) -> str | None:
    """Replace a user's password. Returns an error message, or None on success.

    The current password is required even though the caller is already logged in:
    otherwise anyone who walks up to an unlocked browser could lock the real
    owner out of their own account.
    """
    _require_db()
    if len(new_password) < MIN_PASSWORD_LEN:
        return f"New password must be at least {MIN_PASSWORD_LEN} characters."
    if new_password == current_password:
        return "The new password is the same as the current one."

    user = get_user_by_id(uid)
    if not user:
        return "Account not found."
    if not check_password_hash(user["password_hash"], current_password):
        return "Current password is incorrect."

    r = requests.patch(
        f"{_SUPABASE_URL}/rest/v1/users",
        params={"id": f"eq.{uid}"},
        headers=_sb_headers(),
        json={"password_hash": generate_password_hash(new_password)},
        timeout=15,
    )
    r.raise_for_status()
    return None


def touch_last_login(uid: str) -> None:
    try:
        requests.patch(
            f"{_SUPABASE_URL}/rest/v1/users",
            params={"id": f"eq.{uid}"},
            headers=_sb_headers(),
            json={"last_login_at": datetime.now(timezone.utc).isoformat()},
            timeout=10,
        )
    except Exception as e:
        print(f"[auth] last_login update failed (ignored): {e}")


# ---------------------------------------------------------------------------
# Session guard
# ---------------------------------------------------------------------------

def current_user() -> dict | None:
    uid = session.get("uid")
    if not uid:
        return None
    try:
        return get_user_by_id(uid)
    except Exception as e:
        print(f"[auth] could not load session user: {e}")
        return None


def _wants_html() -> bool:
    return "text/html" in (request.headers.get("Accept") or "")


def login_required(fn):
    """Redirect browsers to the login page; give the JSON/SSE endpoints a 401 so
    the frontend can bounce to /login instead of rendering an empty panel."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            if _wants_html():
                return redirect(url_for("login", next=request.path))
            return jsonify({"error": "auth", "message": "Login required"}), 401
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Chat log (write-only)
# ---------------------------------------------------------------------------

def log_chat(uid: str, role: str, content: str) -> None:
    """Record one chat turn. Never raises — a logging failure must not break the
    user's chat response."""
    if not (_SUPABASE_OK and uid and content):
        return
    try:
        r = requests.post(
            f"{_SUPABASE_URL}/rest/v1/chat_messages",
            headers=_sb_headers(),
            json=[{"user_id": uid, "role": role, "content": content}],
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[chat-log] insert failed (ignored): {e}")


# ---------------------------------------------------------------------------
# One-time migration off the pre-accounts global row
# ---------------------------------------------------------------------------

def bootstrap_owner() -> dict:
    """Create the owner account and copy the legacy app_state portfolio to it.

    Idempotent: does nothing once an owner exists. app_state is read but never
    modified or deleted, so it stays available as a rollback path.
    """
    _require_db()
    existing = get_owner()
    if existing:
        return {"created": False, "user_id": existing["id"],
                "email": existing["email"], "message": "Owner already exists."}

    email = os.getenv("OWNER_EMAIL")
    password = os.getenv("OWNER_PASSWORD")
    if not (email and password):
        raise RuntimeError("Set OWNER_EMAIL and OWNER_PASSWORD before bootstrapping.")

    user = create_user(email, password, is_owner=True)

    # Raises if Supabase is unreachable — better to fail loudly than to seed the
    # owner with an empty portfolio and have them think their data is gone.
    legacy = _sb_load()
    copied = 0
    if legacy:
        _sb_save_user(user["id"], legacy)
        copied = len(legacy.get("holdings", []))

    return {
        "created": True,
        "user_id": user["id"],
        "email": user["email"],
        "holdings_copied": copied,
        "message": f"Owner created; copied {copied} holding(s) from app_state.",
    }
