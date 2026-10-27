"""Single-admin authentication: bcrypt password from .env, signed-cookie session, login rate limit."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import Request

from app.config import settings

MAX_ATTEMPTS = 5
WINDOW_S = 60.0
_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


class NotAuthenticated(Exception):
    pass


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > 72:
        raise ValueError("A password não pode exceder 72 bytes.")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash or len(password.encode("utf-8")) > 72:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def check_credentials(username: str, password: str) -> bool:
    return username == settings.admin_username and verify_password(password, settings.admin_password_hash)


def rate_limited(client_ip: str) -> bool:
    now = time.monotonic()
    with _lock:
        bucket = _attempts[client_ip]
        while bucket and now - bucket[0] > WINDOW_S:
            bucket.popleft()
        if len(bucket) >= MAX_ATTEMPTS:
            return True
        bucket.append(now)
        return False


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    return user


def login(request: Request, username: str) -> None:
    request.session.clear()
    request.session["user"] = username


def logout(request: Request) -> None:
    request.session.clear()
