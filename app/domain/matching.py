"""Match one listing (or property) against one buyer's criteria.

Three outcome sets, never collapsed (the promise in the proposal and in the follow-up message):
  satisfied    checks that passed
  unsatisfied  checks that failed -> the listing is NOT a match
  unknown      the listing does not say -> shown as "não indicado", never counted as confirmed

Grade: "confirmada" when everything passed and nothing is unknown; "possivel" when it passed but
some check is unknown. `criteria.unknown_policy == "hide"` rejects an ad that does not state an obligatory feature;
price, zone or typology left unknown still show as "possivel" (that is what "obrigatorio" meant in the message to her).
Hard checks are deterministic rules only; no LLM is involved anywhere in matching.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.listing import POSITIVE_TIERS, TIER_ABSENT, TIER_UNKNOWN, feature_tier, normalize_text
from app.geo.resolve import in_any_zone, is_within

GRADE_CONFIRMED = "confirmada"
GRADE_POSSIBLE = "possivel"


@dataclass
class MatchResult:
    ok: bool
    grade: str | None
    satisfied: list[str] = field(default_factory=list)
    unsatisfied: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _area(listing: Any) -> float | None:
    return listing.area_util if listing.area_util is not None else listing.area_bruta


def zone_verdict(geo_code: str | None, zone_codes: list[str]) -> bool | None:
    """True inside a zone; None when the listing is only located at a coarser level than the zone asks for
    (an alert email saying just "Lisboa" against a concelho zone: "não indicado", not a rejection); False otherwise.
    A listing with no location at all is a fail, or it would surface for every buyer everywhere."""
    if not geo_code:
        return False
    if in_any_zone(geo_code, zone_codes):
        return True
    if any(is_within(z, geo_code) for z in zone_codes):
        return None
    return False


def evaluate(listing: Any, criteria: Any, zone_codes: list[str]) -> MatchResult:
    """`listing` and `criteria` are duck-typed: ORM rows or dataclasses with the same attribute names."""
    satisfied: list[str] = []
    unsatisfied: list[str] = []
    unknown: list[str] = []

    def check(name: str, verdict: bool | None) -> None:
        (satisfied if verdict is True else unsatisfied if verdict is False else unknown).append(name)

    check("ativo", getattr(listing, "status", "active") == "active")
    check("transacao", listing.transaction == criteria.transaction)
    check("tipo", None if listing.estate_type is None else listing.estate_type in criteria.estate_types)
    check("zona", zone_verdict(listing.geo_code, zone_codes))

    if listing.rooms is None:
        check("tipologia", None)
    else:
        lo_ok = criteria.typology_min is None or listing.rooms >= criteria.typology_min
        hi_ok = criteria.typology_max is None or listing.rooms <= criteria.typology_max
        check("tipologia", lo_ok and hi_ok)

    if listing.price is None or getattr(listing, "hide_price", False):
        check("preco", None)
    else:
        lo_ok = criteria.price_min is None or listing.price >= criteria.price_min
        hi_ok = criteria.price_max is None or listing.price <= criteria.price_max
        check("preco", lo_ok and hi_ok)

    if criteria.area_min:
        area = _area(listing)
        check("area", None if area is None else area >= criteria.area_min)

    if getattr(listing, "is_private_owner", False) and not criteria.include_private_owner:
        check("particular", False)
    if getattr(listing, "is_new_development", False) and not criteria.include_new_developments:
        check("empreendimento", False)

    if criteria.exclude:
        haystack = normalize_text(" ".join(filter(None, [getattr(listing, "title", None), getattr(listing, "description", None)])))
        hits = [term for term in criteria.exclude if normalize_text(term) and normalize_text(term) in haystack]
        if hits:
            check("excluir:" + ",".join(hits), False)

    features = getattr(listing, "features", None) or {}
    for key in criteria.must_have:
        tier = feature_tier(features, key)
        if tier in POSITIVE_TIERS:
            check(key, True)
        elif tier == TIER_ABSENT:
            check(key, False)
        else:
            check(key, None)
            if criteria.unknown_policy == "hide":
                unsatisfied.append(f"{key}:nao-indicado")  # obligatory and not stated: hidden, as promised to her
    for key in criteria.nice_to_have:
        if feature_tier(features, key) in POSITIVE_TIERS:
            satisfied.append(f"+{key}")

    ok = not unsatisfied
    grade = None if not ok else (GRADE_CONFIRMED if not unknown else GRADE_POSSIBLE)
    return MatchResult(ok=ok, grade=grade, satisfied=satisfied, unsatisfied=unsatisfied, unknown=unknown)


def tier_label(features: dict | None, key: str) -> str:
    """Portuguese label for the panel and Telegram: confirmado / mencionado no texto / não indicado / não tem."""
    tier = feature_tier(features, key)
    return {"confirmed": "confirmado", "mentioned": "mencionado no texto", "absent": "não tem", TIER_UNKNOWN: "não indicado"}[tier]
