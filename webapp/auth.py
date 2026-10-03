"""Master-password login.

First visit: if no master password exists yet, the login page asks to create one
(or it is seeded from the MASTER_PASSWORD environment variable). Only a salted hash is
stored. Failed attempts are throttled per IP. Every form carries a CSRF token.
"""
from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from functools import wraps

from flask import abort, flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

KEY = "master_password_hash"
MIN_LENGTH = 8
MAX_FAILS, WINDOW = 5, 600          # 5 wrong tries per 10 minutes per IP
_fails: dict[str, deque] = defaultdict(deque)


def secret_key() -> str:
    """Flask secret key: env SECRET_KEY, else one generated once and kept in the DB."""
    env = os.getenv("SECRET_KEY")
    if env:
        return env
    key = db.get_setting("secret_key")
    if not key:
        key = secrets.token_hex(32)
        db.set_setting("secret_key", key)
    return key


def seed_from_env() -> None:
    pw = os.getenv("MASTER_PASSWORD", "")
    if pw and not has_password():
        set_password(pw)


def has_password() -> bool:
    return bool(db.get_setting(KEY))


def set_password(pw: str) -> None:
    db.set_setting(KEY, generate_password_hash(pw))


def check_password(pw: str) -> bool:
    h = db.get_setting(KEY)
    return bool(h) and check_password_hash(h, pw)


def _ip() -> str:
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "?")
            .split(",")[0].strip())


def locked_out() -> int:
    """Seconds until this IP may try again (0 = allowed)."""
    q, now = _fails[_ip()], time.time()
    while q and now - q[0] > WINDOW:
        q.popleft()
    return int(WINDOW - (now - q[0])) + 1 if len(q) >= MAX_FAILS else 0


def record_fail() -> None:
    _fails[_ip()].append(time.time())


def login_user() -> None:
    session.clear()
    session.permanent = True
    session["auth"] = True
    session["csrf"] = secrets.token_urlsafe(24)


def logged_in() -> bool:
    return bool(session.get("auth"))


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not logged_in():
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("main.login", next=request.path))
        return view(*a, **kw)
    return wrapper


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf() -> None:
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            flash("Your session expired. Please try again.", "error")
            abort(400)
