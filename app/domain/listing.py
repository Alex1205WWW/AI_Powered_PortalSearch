"""Canonical listing shape produced by every adapter, and feature confidence tiers.

Tiers (Roadmap_M1.md §3 item 12):
  confirmed  typed field on the source says yes (RE/MAX `elevator`, Imovirtual features entry)
  mentioned  keyword found in title/description by plain regex (never an LLM)
  absent     typed field on the source says no
  unknown    nothing on the listing says anything -> shown as "não indicado", never a confirmed match

Conventions shared by every adapter:
  rooms  = Portuguese typology T (bedrooms). Imovirtual's `roomsNumber` counts divisions, so the adapter
           subtracts one (verified 2026-10-28). T10+ is stored as 10.
  floor  = -1 cave, 0 rés-do-chão, 1..10, 11 = above the tenth. None when the source does not say.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

TIER_CONFIRMED = "confirmed"
TIER_MENTIONED = "mentioned"
TIER_ABSENT = "absent"
TIER_UNKNOWN = "unknown"
POSITIVE_TIERS = {TIER_CONFIRMED, TIER_MENTIONED}
TYPED_TIERS = {TIER_CONFIRMED, TIER_ABSENT}

# Matched against accent-stripped, lower-cased text.
FEATURE_PATTERNS: dict[str, re.Pattern] = {
    "garagem": re.compile(r"\bgaragem\b|\bbox\b|lugar(?:es)? de (?:garagem|estacionamento)|\bparqueamento\b"),
    "elevador": re.compile(r"\belevador(?:es)?\b"),
    "exterior": re.compile(r"\bterreno\b|\bquintal\b|\blogradouro\b|\bjardim\b|espaco exterior|\bpatio\b|\bterraco\b|\bterracos\b"),
    "varanda": re.compile(r"\bvarandas?\b"),
    "terraco": re.compile(r"\bterracos?\b"),
    "jardim": re.compile(r"\bjardim\b"),
    "piscina": re.compile(r"\bpiscina\b"),
    "arrecadacao": re.compile(r"\barrecadacao\b"),
    "vista": re.compile(r"\bvistas? (?:de |para o |para a |sobre o |sobre a )?(?:mar|rio|tejo)\b"),
}


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", stripped.lower())


def features_from_text(*texts: str | None) -> dict[str, dict]:
    """Keyword hits -> {"garagem": {"value": True, "tier": "mentioned"}, ...}"""
    haystack = normalize_text(" ".join(t for t in texts if t))
    return {key: {"value": True, "tier": TIER_MENTIONED} for key, pattern in FEATURE_PATTERNS.items() if pattern.search(haystack)}


def merge_features(typed: dict[str, bool | None] | None, text_hits: dict[str, dict] | None) -> dict[str, dict]:
    """Typed source fields win over keyword hits. None in `typed` means the source did not say."""
    merged: dict[str, dict] = dict(text_hits or {})
    for key, value in (typed or {}).items():
        if value is True:
            merged[key] = {"value": True, "tier": TIER_CONFIRMED}
        elif value is False:
            merged[key] = {"value": False, "tier": TIER_ABSENT}
    return merged


def feature_tier(features: dict[str, dict] | None, key: str) -> str:
    entry = (features or {}).get(key)
    return entry["tier"] if entry else TIER_UNKNOWN


CANARY_MIN_SAMPLE = 30
CANARY_THRESHOLDS = {"agency": 0.85, "area": 0.90, "rooms": 0.90, "published": 0.90}  # plan Day 2 said date 100 %; real ads carry occasional impossible dates, so 90 % (live fill 2026-10-28: 100 / 100 / 99.5 / 99.4 %)

# Which canary key each profile field promises. Agency sites publish far less than a portal: eGO types no
# area at all and carries no publication date, so holding them to the Imovirtual contract would flag every
# eGO source as `degraded` on its first run (Plan_Day5.md section 5 item 1).
CANARY_FROM_PROFILE_FIELD = {"area": "area", "typology": "rooms", "published": "published"}


def canary_fields(fields_present: Iterable[str] | None) -> set[str]:
    """The canary keys a source has actually promised. A field the profile never promised cannot be drift.

    `agency` is always expected on an agency site: the advertiser is the site owner, so a listing without one
    really is a parsing failure."""
    promised = set(fields_present or ())
    keys = {"agency"}
    keys.update(canary for field, canary in CANARY_FROM_PROFILE_FIELD.items() if field in promised)
    return keys


def canary_fields_for(source_kind: str | None, profile: dict | None) -> set[str] | None:
    """None = the full portal contract (every threshold applies). A set = only what this source promised."""
    if source_kind != "agency":
        return None
    return canary_fields((profile or {}).get("fields_present"))


class FillStats:
    """Parser-drift canary: field-fill ratios over the listings one run produced. A source that suddenly returns
    listings without agency, area, typology or date is almost always a changed page layout, not a changed market.

    `expect` narrows the check to the fields a source promised; None keeps the full portal contract."""

    def __init__(self, expect: Iterable[str] | None = None) -> None:
        self.n = 0
        self.expect = set(CANARY_THRESHOLDS) if expect is None else {k for k in expect if k in CANARY_THRESHOLDS}
        self.hits = {key: 0 for key in CANARY_THRESHOLDS}

    def add(self, c: "CanonicalListing") -> None:
        self.n += 1
        self.hits["agency"] += bool(c.agency_name)
        self.hits["area"] += c.area_util is not None or c.area_bruta is not None
        self.hits["rooms"] += c.rooms is not None
        self.hits["published"] += c.published_at is not None or c.published_unknown  # a placeholder date is a present field, not drift

    def ratios(self) -> dict[str, float]:
        return {key: (self.hits[key] / self.n if self.n else 0.0) for key in CANARY_THRESHOLDS}

    def breaches(self) -> list[str]:
        if self.n < CANARY_MIN_SAMPLE:
            return []
        return [f"{key} {ratio:.0%} < {CANARY_THRESHOLDS[key]:.0%}" for key, ratio in self.ratios().items()
                if key in self.expect and ratio < CANARY_THRESHOLDS[key]]


@dataclass
class CanonicalListing:
    """What an adapter hands to the persistence layer. Field names mirror `db.models.Listing`."""

    source_key: str
    external_id: str
    url: str
    title: str | None = None
    price: int | None = None
    currency: str = "EUR"
    hide_price: bool = False
    area_util: float | None = None
    area_bruta: float | None = None
    land_area: float | None = None
    rooms: int | None = None
    wc: int | None = None
    floor: int | None = None
    estate_type: str | None = None
    transaction: str = "sale"
    geo_code: str | None = None
    geo_path: list[tuple[str, str, str]] = field(default_factory=list)  # (code, level, name) from district down
    street: str | None = None
    lat: float | None = None
    lng: float | None = None
    agency_name: str | None = None
    agency_phone: str | None = None
    agency_slug: str | None = None
    agency_urn: str | None = None
    reference: str | None = None
    is_private_owner: bool = False
    is_new_development: bool = False
    features: dict[str, dict] = field(default_factory=dict)
    images: list[str] = field(default_factory=list)
    description: str | None = None
    published_at: datetime | None = None
    published_unknown: bool = False  # the source carried a date field but marked it unknown (Imovirtual: 1999-02-29 00:00:01)
    previous_price: int | None = None
    price_reduced_at: datetime | None = None  # set only when the source states the previous price was a REDUCTION
    status: str = "active"
    raw: dict = field(default_factory=dict)

    def missing_fields(self) -> list[str]:
        missing = []
        if self.price is None or self.hide_price:
            missing.append("preco")
        if self.area_util is None and self.area_bruta is None:
            missing.append("area")
        if self.rooms is None:
            missing.append("tipologia")
        if self.geo_code is None:
            missing.append("zona")
        return missing
