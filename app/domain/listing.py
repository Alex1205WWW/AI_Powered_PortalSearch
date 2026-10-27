"""Canonical listing shape produced by every adapter, and feature confidence tiers.

Tiers (Roadmap_M1.md §3 item 12):
  confirmed  typed field on the source says yes (RE/MAX `elevator`, Imovirtual features entry)
  mentioned  keyword found in title/description by plain regex (never an LLM)
  absent     typed field on the source says no
  unknown    nothing on the listing says anything -> shown as "não indicado", never a confirmed match
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

TIER_CONFIRMED = "confirmed"
TIER_MENTIONED = "mentioned"
TIER_ABSENT = "absent"
TIER_UNKNOWN = "unknown"
POSITIVE_TIERS = {TIER_CONFIRMED, TIER_MENTIONED}

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
    previous_price: int | None = None
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
