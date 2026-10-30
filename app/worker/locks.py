"""Postgres advisory locks so two workers (or an overlapping job) never run the same source at once.

The lock lives on its own autocommit connection for the whole job. A session-level advisory lock belongs to the
connection that took it, and a job commits several times, which hands the session's connection back to the pool;
unlocking through the session would then hit a different connection and the lock would stay held for good
(verified 2026-10-28: after one commit the lock leaked and every later acquire returned False)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

from app.db.session import engine


@contextmanager
def advisory_lock(name: str) -> Iterator[bool]:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": name}).scalar())
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": name})
