"""Seed the static geo taxonomy (3 districts + 50 concelhos) from data/geo_pt.csv. Parishes are learned
lazily from sources (Imovirtual reverseGeocoding, RE/MAX regionName) via `ensure_parish`."""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Geo
from app.geo.resolve import child_code

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "geo_pt.csv"


def seed_geo(session: Session, path: Path = DATA_FILE) -> int:
    count = 0
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            session.merge(Geo(code=row["code"], level=row["level"], name=row["name"], parent_code=row["parent_code"] or None))
            count += 1
    session.flush()
    return count


def ensure_parish(session: Session, council_code: str, parish_name: str) -> str:
    code = child_code(council_code, parish_name)
    if session.get(Geo, code) is None:
        session.add(Geo(code=code, level="parish", name=parish_name.strip(), parent_code=council_code))
        session.flush()
    return code
