"""Postgres advisory locks so two workers (or an overlapping job) never run the same source at once."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session


@contextmanager
def advisory_lock(session: Session, name: str) -> Iterator[bool]:
    acquired = bool(session.execute(text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": name}).scalar())
    try:
        yield acquired
    finally:
        if acquired:
            session.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": name})
