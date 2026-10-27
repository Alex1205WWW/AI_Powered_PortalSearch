"""Geo helpers. Codes are hierarchical paths (`lisboa`, `lisboa/oeiras`, `lisboa/oeiras/alges`), so
"is this listing inside that zone" is a prefix test and no recursive SQL is needed."""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Geo

LEVELS = {1: "district", 2: "council", 3: "parish"}


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def level_of(code: str) -> str:
    return LEVELS[min(code.count("/") + 1, 3)]


def ancestors(code: str) -> list[str]:
    parts = code.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def is_within(code: str, zone_code: str) -> bool:
    return code == zone_code or code.startswith(zone_code + "/")


def in_any_zone(code: str | None, zone_codes: list[str]) -> bool:
    return bool(code) and any(is_within(code, z) for z in zone_codes)


def find(session: Session, text: str, level: str | None = None) -> list[Geo]:
    """All geo rows whose last code segment or name matches the text. Several results = ambiguity
    (e.g. "Lisboa" is both a district and a council); the caller must ask, never guess."""
    slug = slugify(text)
    if not slug:
        return []
    stmt = select(Geo).where(or_(Geo.code == slug, Geo.code.like(f"%/{slug}")))
    if level:
        stmt = stmt.where(Geo.level == level)
    return list(session.scalars(stmt.order_by(Geo.code)))


def child_code(parent_code: str, name: str) -> str:
    return f"{parent_code}/{slugify(name)}"
