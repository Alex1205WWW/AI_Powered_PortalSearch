"""Property identity rules (Roadmap_M1.md §3 item 11, revised 2026-10-28 on real data). Pure functions, no I/O.

Two listings are the same property when, inside the blocking key, either
  A. both carry the same (agency, reference), or
  B. their photos match: pHash Hamming <= 8 on enough image pairs, only across different sources, never when the
     floors are both known and differ, never for new developments, and only when exactly one property qualifies.

Why B is this strict (measured on 1 268 Lisboa listings): photos shared between listings were sibling units in a
building or a row of new townhouses far more often than the same unit twice. A false merge hides a real opportunity,
which is the worse failure for her; a visible duplicate only costs a glance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

AREA_TOL = 0.05
PRICE_TOL = 0.03
HAMMING_MAX = 8
MAX_IMAGES = 3  # first three photos: agencies reuse the same cover photos in the same order on every portal

RULE_REFERENCE = "reference"
RULE_PHOTOS = "photos"
RULE_AMBIGUOUS = "ambiguous"
RULE_NONE = "none"


@dataclass
class Side:
    """What the rules need to know about one listing."""

    listing_id: int
    property_id: int | None
    source_id: int
    council: str | None
    estate_type: str | None
    rooms: int | None
    area: float | None
    price: int | None
    hide_price: bool = False
    floor: int | None = None
    agency_norm: str | None = None
    reference: str | None = None
    is_new_development: bool = False
    hashes: list[int] = field(default_factory=list)  # unsigned 64-bit pHashes of the first images


@dataclass
class Decision:
    property_id: int | None
    rule: str
    note: str = ""


def council_of(geo_code: str | None) -> str | None:
    if not geo_code:
        return None
    parts = geo_code.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def within(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b))


def compatible(a: Side, b: Side) -> bool:
    """Blocking key. A field unknown on either side does not block; precision comes from the identity rules."""
    if not a.council or not b.council or a.council != b.council:
        return False
    if a.estate_type and b.estate_type and a.estate_type != b.estate_type:
        return False
    if a.rooms is not None and b.rooms is not None and a.rooms != b.rooms:
        return False
    if a.area is not None and b.area is not None and not within(a.area, b.area, AREA_TOL):
        return False
    if (a.price is not None and not a.hide_price) and (b.price is not None and not b.hide_price) and not within(a.price, b.price, PRICE_TOL):
        return False
    return True


def floor_contradicts(a: Side, b: Side) -> bool:
    return a.floor is not None and b.floor is not None and a.floor != b.floor


def normalize_reference(ref: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (ref or "").lower())


def same_reference(a: Side, b: Side) -> bool:
    if not (a.agency_norm and b.agency_norm and a.agency_norm == b.agency_norm):
        return False
    ra, rb = normalize_reference(a.reference), normalize_reference(b.reference)
    return bool(ra) and ra == rb


def hamming(x: int, y: int) -> int:
    return bin(x ^ y).count("1")


def matching_pairs(hashes_a: list[int], hashes_b: list[int], max_distance: int = HAMMING_MAX) -> int:
    """Greedy one-to-one pairing of near-identical images between two listings."""
    remaining = list(hashes_b)
    pairs = 0
    for ha in hashes_a:
        for hb in remaining:
            if hamming(ha, hb) <= max_distance:
                remaining.remove(hb)
                pairs += 1
                break
    return pairs


def required_pairs(hashes_a: list[int], hashes_b: list[int]) -> int:
    return 2 if min(len(hashes_a), len(hashes_b)) >= 2 else 1


def photo_match(a: Side, b: Side) -> bool:
    if not a.hashes or not b.hashes:
        return False
    if a.is_new_development or b.is_new_development:
        return False  # identical renders for every unit of a development
    if floor_contradicts(a, b):
        return False
    return matching_pairs(a.hashes, b.hashes) >= required_pairs(a.hashes, b.hashes)


def decide(new: Side, candidates: list[Side]) -> Decision:
    """`candidates` are listings already inside the blocking key and not excluded by a manual split."""
    by_reference = sorted({c.property_id for c in candidates if c.property_id is not None and same_reference(new, c)})
    if by_reference:
        return Decision(by_reference[0], RULE_REFERENCE)
    same_source_properties = {c.property_id for c in candidates if c.source_id == new.source_id}
    by_photos = {
        c.property_id
        for c in candidates
        if c.property_id is not None and c.property_id not in same_source_properties and photo_match(new, c)
    }
    if len(by_photos) == 1:
        return Decision(by_photos.pop(), RULE_PHOTOS)
    if len(by_photos) > 1:
        return Decision(None, RULE_AMBIGUOUS, f"{len(by_photos)} properties share these photos")
    return Decision(None, RULE_NONE)


# --- 64-bit hash storage helpers (Postgres BIGINT is signed) --------------------------------------

def to_signed(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def to_unsigned(value: int) -> int:
    return value + (1 << 64) if value < 0 else value


def phash_of(image_bytes: bytes) -> int:
    """Perceptual hash of an image as an unsigned 64-bit integer (imagehash.phash, 8x8 DCT)."""
    import io

    import imagehash
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        return int(str(imagehash.phash(img)), 16)
