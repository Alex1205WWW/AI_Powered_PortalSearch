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


def _tokens(text: str) -> list[str]:
    return [t for t in slugify(text).split("-") if t]


MAX_PLACE_TOKENS = 5  # "vila-nova-da-barquinha" is the longest council name in her districts


def candidate_slugs(*texts: str | None) -> set[str]:
    """Every contiguous run of up to five tokens in the text, as a slug.

    This is the set a place name could be hiding in. Matching whole runs rather than single tokens is what
    stops "vila-franca-de-xira" being read as "xira"."""
    tokens = _tokens(" ".join(t for t in texts if t))
    out: set[str] = set()
    for width in range(min(MAX_PLACE_TOKENS, len(tokens)), 0, -1):
        for i in range(len(tokens) - width + 1):
            out.add("-".join(tokens[i:i + width]))
    return out


def choose_place(matches: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """Pick one (code, level) from the places a text named. Pure, so it is directly testable.

    Rules (Plan_Day5.md section 3.4):
      * a parish wins, because it is the most precise thing the text said;
      * when several parishes share a name, the one whose own council is also named wins;
      * otherwise prefer the **coarsest** match. That matters for "Lisboa", which is both a district and a
        council: returning the district makes `matching.zone_verdict` answer `unknown` instead of a pass, so
        a property that merely says "Lisboa" is never confirmed into the Lisboa council when it might
        really be in Loures.
    """
    if not matches:
        return None, None
    rank = {"parish": 3, "council": 2, "district": 1}
    parishes = [(code, level) for code, level in matches if level == "parish"]
    if parishes:
        councils = {code for code, level in matches if level == "council"}
        settled = [(c, l) for c, l in parishes if "/".join(c.split("/")[:2]) in councils]
        pool = settled or parishes
        if len(pool) == 1:
            return pool[0]
        ancestors_seen = {"/".join(c.split("/")[:2]) for c, _ in pool}
        if len(ancestors_seen) == 1:
            return ancestors_seen.pop(), "council"
        # Same parish name in two different councils and nothing in the text to separate them. Guessing here
        # could file a Setúbal property under Lisboa, so drop to whatever coarser place the text also named
        # and, failing that, give up: an unresolved zone is `unknown` and shows as "possível", which is
        # recoverable. A wrong zone is not.
        coarser = [m for m in matches if m[1] != "parish"]
        return min(coarser, key=lambda m: (rank.get(m[1], 9), len(m[0]))) if coarser else (None, None)
    return min(matches, key=lambda m: (rank.get(m[1], 9), len(m[0])))


def from_text(session: Session, *texts: str | None) -> tuple[str | None, str | None]:
    """Resolve a free-text location ("Apartamento T3 para venda em Alcântara - Lisboa") to a geo code.

    Portals hand us structured geo; agency sites hand us a title, so `find()` is no use here - it needs the
    place name on its own. The decision itself lives in `choose_place`; this is only the database shell.
    """
    seen = candidate_slugs(*texts)
    if not seen:
        return None, None
    rows = session.scalars(select(Geo).where(or_(Geo.code.in_(seen), *(Geo.code.like(f"%/{s}") for s in seen))))
    # Keep only rows whose own last segment really appears in the text: the LIKE above can over-match.
    matches = [(g.code, g.level) for g in rows if g.code.split("/")[-1] in seen]
    return choose_place(matches)
