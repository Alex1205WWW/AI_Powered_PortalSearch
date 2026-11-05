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


# `listings.price` and `listing_price_history.price` are Postgres `integer`. A price above this cannot be
# stored at all: psycopg raises NumericValueOutOfRange, the transaction dies, and - because ingestion runs
# inside the source's run - the WHOLE pass fails. Seen 2026-11-02: one sintraneves.com listing priced at
# 7 500 000 000 EUR (a genuine industrial portfolio, "10 Fábricas de Álcool / Etanol" - not a typo) left all
# 279 of that site's listings unstored and the source permanently `degraded`.
#
# The boundary is the column's, not a guess about the market: the largest real listing in the database is a
# 69 600 000 EUR Dubai penthouse, three orders of magnitude below the limit, so nothing she could plausibly
# search for is affected. A price that does not fit is treated as "preço sob consulta" - the listing is kept
# and matched on its other fields, the figure is preserved as `raw.price_unstorable`, and `hide_price` makes
# the panel say so instead of showing a blank.
PRICE_MAX = 2**31 - 1


def storable_price(value: int | float | None) -> int | None:
    """The price as it can be stored, or None when the column cannot hold it."""
    if value is None:
        return None
    price = int(value)
    return price if -PRICE_MAX <= price <= PRICE_MAX else None


# A source-reported "previous price" that differs from the current one by a factor of ten or more is a
# scale error in the feed, not a market move. Measured on all 3 277 reported previous prices 2026-11-02:
# every plausible one falls between 0.36x and 2.3x of the current price, and the only values outside that
# band are exact powers of ten with the SAME digits - 830 000 -> 830 000 000 and 2 000 000 -> 2 000 000 000
# (x1000), 439 -> 439 000 (x1/1000), 39 900 -> 399 000 (x1/10). Seven rows in total. A factor of ten is
# therefore ~4x clear of the most extreme real change on either side.
#
# It matters because this row is what her Excel export prints as "preço anterior" (`services/export.py`),
# so an unfiltered feed error reaches the client as "this house used to cost 2 000 000 000 EUR".
PRICE_SCALE_LIMIT = 10


def plausible_previous_price(previous: int | None, current: int | None) -> int | None:
    """A source-reported previous price, or None when the feed's figure is a scale error."""
    if previous is None or not current or previous <= 0:
        return None
    ratio = previous / current
    return previous if 1 / PRICE_SCALE_LIMIT < ratio < PRICE_SCALE_LIMIT else None


# --- sold / reserved stated in the title --------------------------------------------------------
# She asked for this directly on 2026-11-03: "também estão aparecer opções que já dizem 'Reservado',
# essas não é necessário que apareçam".
#
# Some sources carry a typed flag (RE/MAX `isSold`, eGO `availability: soldout`) and those already arrive
# as `status="sold"`. The rest say it only in the title, and 98 listings on production did exactly that
# while still being served as active.
#
# TITLE ONLY, and that is the whole finding. Scanning descriptions was measured on the same data and is
# pure noise: all 14 sampled description hits meant a secluded setting ("ambiente reservado", "espaço
# reservado à privacidade") or a preserved facade ("traça preservada" - "preservad" literally contains
# "reservad"). Filtering on descriptions would have hidden 65 properties, 23 of them her `confirmada`.
#
# The exclusions are not hypothetical either. Of the 98 title hits, 9 use the word as a SALE CONDITION:
#   "2 MORADIAS ... VENDIDAS EM CONJUNTO"        sold as a pair, still for sale      (6)
#   "T1 vendido no estado atual"                 sold as-is                          (2)
#   "... è vendido com todo o recheio"           sold with the contents included     (1)
# Those stay visible. "vendida em projeto" (an off-plan unit that is sold) is NOT excluded.
_SOLD_WORD = r"(?:vendid|reservad)[oa]s?"
_SALE_CONDITION = r"(?:em\s+conjunto|conjunto|no\s+estado|com\b|por\b)"
# The lookbehind is what keeps "preservado" out: in that word the character before "reservad" is a letter.
SOLD_TITLE_RE = re.compile(rf"(?<![a-zA-Z]){_SOLD_WORD}(?![a-zA-Z])(?!\s*{_SALE_CONDITION})",
                           re.IGNORECASE)

# "reservado" also means secluded, and then it describes a noun rather than the sale. No production title
# does this today (all 3 real "reservad" titles are genuine markers) but plenty of descriptions do, so a
# title like "Moradia T3 em ambiente reservado" is entirely plausible and must stay visible. Removing the
# phrase before testing is clearer than stacking lookbehinds, and keeps the sale rule readable.
_SECLUDED_RE = re.compile(
    r"\b(?:ambientes?|espa[çc]os?|zonas?|[áa]reas?|locais|local|condom[íi]nios?|utiliza[çc][ãa]o|uso|"
    r"recantos?|p[áa]tios?|jardins?|jardim|terra[çc]os?|salas?|quartos?|su[íi]tes?|varandas?)"
    r"\s+(?:muito\s+|mais\s+|bastante\s+|t[ãa]o\s+)?reservad[oa]s?\b",
    re.IGNORECASE)


def sold_from_title(title: str | None) -> bool:
    """True when the title itself says the property is sold or reserved."""
    if not title:
        return False
    return SOLD_TITLE_RE.search(_SECLUDED_RE.sub(" ", title)) is not None


# --- agency phone numbers -----------------------------------------------------------------------
# She hands this column to someone who dials it, so a number that cannot be called is worse than a blank:
# it spends a paid assistant's time before it fails.
#
# Measured on all 1 522 numbers held for her shown properties (2026-11-05): 0 had the wrong length and
# only 62 were junk, all from two directory entries - `990000000` on an "agency" called OLX, and
# `210000000` on one literally named "12 12 12". So the filter below is deliberately narrow: two patterns
# that were actually observed, nothing invented.
#
# What it must NOT reject, because these looked wrong and are real Portuguese numbers:
#   808 200 030   shared-cost "linha azul" (RE/MAX + Grupo Vantagem)
#   308 806 576   the 30x nomadic / VoIP range, used by several agencies
_PHONE_JUNK = (
    re.compile(r"^9[^1236]"),   # mobiles are 91/92/93/96; 99… is not a real prefix
    re.compile(r"0{6}"),        # six zeros in a row is a placeholder, never a number
)


def normalize_phone(value: str | None) -> str | None:
    """Digits only, without the +351 country code, so formatting never counts as a difference."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    digits = digits[3:] if digits.startswith("351") and len(digits) > 9 else digits
    return digits or None


def plausible_phone(value: str | None) -> str | None:
    """The number if it could actually be dialled, else None."""
    digits = normalize_phone(value)
    if digits is None or len(digits) != 9:
        return None
    return None if any(pattern.search(digits) for pattern in _PHONE_JUNK) else digits


CANARY_MIN_SAMPLE = 30
CANARY_THRESHOLDS = {"agency": 0.85, "area": 0.90, "rooms": 0.90, "published": 0.90}  # plan Day 2 said date 100 %; real ads carry occasional impossible dates, so 90 % (live fill 2026-10-28: 100 / 100 / 99.5 / 99.4 %)

# Agency sites are mixed catalogues, not curated portal feeds: houses are often listed without a T-number,
# land has none at all, and eGO never types an area. Measured on 44 live sites 2026-11-02, healthy readers
# fill `rooms` anywhere between 44 % and 100 % and `area` between 27 % and 100 %, so the portal thresholds
# flagged five correctly-working sites and put a false "problemas" banner on every page. A layout change takes
# a field to ~0 %, which half still catches; the canary stays a smoke detector without becoming a fire alarm.
AGENCY_CANARY_THRESHOLDS = {"area": 0.50, "rooms": 0.50, "published": 0.50}

# Which canary key each profile field promises. Agency sites publish far less than a portal: eGO types no
# area at all and carries no publication date, so holding them to the Imovirtual contract would flag every
# eGO source as `degraded` on its first run (Plan_Day5.md section 5 item 1).
CANARY_FROM_PROFILE_FIELD = {"area": "area", "typology": "rooms", "published": "published"}


def canary_fields(fields_present: Iterable[str] | None) -> set[str]:
    """The canary keys a source has actually promised. A field the profile never promised cannot be drift.

    `agency` is deliberately NOT among them. On an agency's own site the advertiser is the site itself, and
    whether a page repeats that name is a schema choice, not a reading result: measured 2026-11-02, all six
    JSON-LD sites carry no per-advert agent block (0 %) while every eGO site carries one (100 %). Both read
    correctly. The agency is known from the `sources` row, so a 0 % here is not drift."""
    promised = set(fields_present or ())
    return {canary for field, canary in CANARY_FROM_PROFILE_FIELD.items() if field in promised}


def canary_fields_for(source_kind: str | None, profile: dict | None) -> set[str] | None:
    """None = the full portal contract (every threshold applies). A set = only what this source promised."""
    if source_kind != "agency":
        return None
    return canary_fields((profile or {}).get("fields_present"))


def canary_thresholds_for(source_kind: str | None) -> dict[str, float]:
    return AGENCY_CANARY_THRESHOLDS if source_kind == "agency" else CANARY_THRESHOLDS


class FillStats:
    """Parser-drift canary: field-fill ratios over the listings one run produced. A source that suddenly returns
    listings without agency, area, typology or date is almost always a changed page layout, not a changed market.

    `expect` narrows the check to the fields a source promised; None keeps the full portal contract."""

    def __init__(self, expect: Iterable[str] | None = None, thresholds: dict[str, float] | None = None) -> None:
        self.n = 0
        self.thresholds = CANARY_THRESHOLDS if thresholds is None else thresholds
        self.expect = set(self.thresholds) if expect is None else {k for k in expect if k in self.thresholds}
        self.hits = {key: 0 for key in CANARY_THRESHOLDS}

    def add(self, c: "CanonicalListing") -> None:
        self.n += 1
        self.hits["agency"] += bool(c.agency_name)
        # A plot's size is its land area, and a plot has no typology: neither is a field the page failed to give.
        self.hits["area"] += c.area_util is not None or c.area_bruta is not None or c.land_area is not None
        self.hits["rooms"] += c.rooms is not None or c.estate_type == "terreno"
        self.hits["published"] += c.published_at is not None or c.published_unknown  # a placeholder date is a present field, not drift

    def ratios(self) -> dict[str, float]:
        return {key: (self.hits[key] / self.n if self.n else 0.0) for key in CANARY_THRESHOLDS}

    def breaches(self) -> list[str]:
        if self.n < CANARY_MIN_SAMPLE:
            return []
        return [f"{key} {ratio:.0%} < {self.thresholds[key]:.0%}" for key, ratio in self.ratios().items()
                if key in self.expect and ratio < self.thresholds[key]]


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

    def __post_init__(self) -> None:
        # Applied here so no adapter can forget it, and so a source that already reports a typed status
        # always wins: only an otherwise-active listing is downgraded. `matching.evaluate` already rejects
        # anything that is not active, so this is all it takes for her to stop seeing sold listings.
        if self.status == "active" and sold_from_title(self.title):
            self.status = "sold"
        # A text field that is handed a flag by mistake reaches the database as "true" and is printed to
        # her as an address. That is how 9 842 RE/MAX listings came to have the street "true": the adapter
        # read `publicAddress`, which is a boolean. Fixed at the adapter, and fixed here too, so no future
        # adapter can repeat it - a value that is not text is not an address.
        if self.street is not None and not isinstance(self.street, str):
            self.street = None

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
