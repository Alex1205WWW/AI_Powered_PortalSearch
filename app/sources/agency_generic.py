"""The generic agency-site reader: one interpreter of the declarative profile in `sources.profile`.

There is deliberately no per-site code here. `agency_profiler` writes a profile (platform, index URLs,
listing-URL regex, incremental strategy, extraction engine, image size rewrite); this module executes it.
Adding support for another platform means teaching the profiler a fingerprint, not writing an adapter —
which is the only way "sem limite de sites" can be true for the ~213 readable agencies measured on
2026-10-31 (`Milestone1/Plan_Day5.md` §2, §3.1).

The same `extract()` the reader uses is what the profiler measures confidence with, so a site's stated
confidence can never drift from what the reader really gets out of it.

Contracts verified live 2026-10-31 (`Plan_Day5.md` §1):
- eGO (the most common PT agency platform): `/imovel/<slug>/<id>`, JSON-LD `RealEstateListing` +
  `RealEstateAgent`, price typed but often absent, **area and typology never typed**, sitemap carries
  **no `<lastmod>`**, withdrawn listing answers **410**, images resizable via the `Z<W>x<H>` path segment.
- Own-platform sites (portadafrente, grupovantagem) type far more: `numberOfBedrooms`, `floorSize`,
  `amenityFeature[]`, `geo`, `datePosted`. grupovantagem wraps its JSON-LD in `@graph` and keeps the price
  on a separate `Offer` node, so the graph must be flattened before anything is read.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from app.domain.listing import (CanonicalListing, TIER_CONFIRMED, features_from_text, merge_features,
                                normalize_text)

log = logging.getLogger(__name__)

LD_JSON_RE = re.compile(r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
META_RE = re.compile(r'<meta[^>]+(?:property|name)\s*=\s*["\']([^"\']+)["\'][^>]*content\s*=\s*["\']([^"\']*)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")

# Portuguese typology. "T1+1" (one bedroom plus one convertible room) is written `t11` in an eGO slug, so a
# naive `t(\d+)` reads eleven bedrooms. Measured 2026-10-31 over ~13 500 eGO slugs: 950 (7 %) are compounds
# (t11 229, t12 123, t21 213, t31 177, t41 94, t51 29 … t82), against a single genuine t10. The title spells
# the compound out, so the title wins and the slug is only trusted for a single digit.
TYPOLOGY_TEXT_RE = re.compile(r"\bt\s*(\d{1,2})\s*(?:\+\s*(\d{1,2}))?\b", re.I)
TYPOLOGY_SLUG_RE = re.compile(r"-t(\d)(?:-|$)")
AREA_RE = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:m2|m²|metros quadrados)", re.I)
# "área bruta privativa de 177 m2" / "177 m2 de área" -> prefer a number that sits next to the word "área".
AREA_NEAR_WORD_RE = re.compile(r"(?:area[^.]{0,24}?(\d{1,4}(?:[.,]\d{1,2})?)\s*m2|(\d{1,4}(?:[.,]\d{1,2})?)\s*m2[^.]{0,16}?area)", re.I)

ESTATE_FROM_TEXT = (
    ("apartamento", re.compile(r"\bapartament|\bduplex\b|\bflat\b|\bpenthouse\b|\bloft\b")),
    ("moradia", re.compile(r"\bmoradia|\bvilla\b|\bcasa\b|\btownhouse\b|\bquinta\b|\bherdade\b|\bpredio\b")),
    ("terreno", re.compile(r"\bterreno\b|\blote\b")),
)
SELL_MARKERS = re.compile(r"#sell|\bsell\b|\bvenda\b|\bsale\b", re.I)
RENT_MARKERS = re.compile(r"#leaseout|\brent\b|\barrendamento\b|\baluguer\b", re.I)
GONE_STATUS = {404, 410}  # eGO answers 410 on a withdrawn listing (verified live 2026-10-31)

# New developments matter twice over: `criteria.include_new_developments` filters them, and `dedup.photo_match`
# refuses to merge them because every unit of a development is marketed with the same renders. Agency sites
# sell a lot of them - "novo empreendimento" is one of the commonest phrases in the eGO slugs.
DEVELOPMENT_RE = re.compile(r"novo empreendimento|empreendimento nov|\bempreendimento\b|em construcao|nova construcao|"
                            r"pre-?lancamento|\bplanta\b|\bresidences\b|\bresort\b")

# schema.org amenityFeature / LocationFeatureSpecification names -> our feature keys. Matched against
# accent-stripped lower-case text, so "Arrecadação" arrives as "arrecadacao".
AMENITY_TO_FEATURE = {
    "garagem": ("garagem", "box", "estacionamento", "parqueamento", "lugar de garagem"),
    "elevador": ("elevador",),
    "varanda": ("varanda", "varandas"),
    "terraco": ("terraco", "terracos"),
    "jardim": ("jardim",),
    "piscina": ("piscina",),
    "arrecadacao": ("arrecadacao", "arrumos"),
}


# --- JSON-LD -----------------------------------------------------------------------------------

def _unescape(value):
    """eGO emits HTML numeric entities inside its JSON-LD strings (`Alc&#226;ntara`) in an otherwise valid
    UTF-8 document, so every string has to be unescaped after `json.loads` and before geo resolution."""
    if isinstance(value, str):
        return html_lib.unescape(value)
    if isinstance(value, list):
        return [_unescape(v) for v in value]
    if isinstance(value, dict):
        return {k: _unescape(v) for k, v in value.items()}
    return value


def jsonld_nodes(page: str) -> list[dict]:
    """Every JSON-LD object on the page, flattened: lists and `@graph` wrappers are expanded, because
    grupovantagem keeps the listing in `@graph` and its price on a sibling `Offer` node."""
    nodes: list[dict] = []

    def walk(obj) -> None:
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            if "@graph" in obj:
                walk(obj["@graph"])
            nodes.append(obj)
            for key in ("offers", "about", "containedInPlace", "provider", "mainEntity"):
                if isinstance(obj.get(key), (dict, list)):
                    walk(obj[key])

    for block in LD_JSON_RE.findall(page):
        try:
            walk(_unescape(json.loads(block)))
        except (json.JSONDecodeError, TypeError):
            continue
    return nodes


def _types(node: dict) -> set[str]:
    value = node.get("@type") or ""
    return {v.lower() for v in ([value] if isinstance(value, str) else value)}


def _first(nodes: list[dict], *wanted: str) -> dict | None:
    want = {w.lower() for w in wanted}
    return next((n for n in nodes if _types(n) & want), None)


def _number(value) -> float | None:
    """Tolerant numeric read. Handles `2500000`, `"2500000"`, `"504.000 €"`, `"177,5"` and
    `{"@type": "QuantitativeValue", "value": 65}`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return _number(value.get("value"))
    if not isinstance(value, str):
        return None
    text = re.sub(r"[^\d.,]", "", value)
    if not text:
        return None
    if "," in text and "." in text:          # "504.000,50" -> thousands dot, decimal comma
        text = text.replace(".", "").replace(",", ".")
    elif text.count(",") == 1:               # "177,5" -> decimal comma
        text = text.replace(",", ".")
    elif text.count(".") > 1:                # "504.000" -> thousands dots
        text = text.replace(".", "")
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def metas(page: str) -> dict[str, str]:
    return {k.lower(): html_lib.unescape(v) for k, v in META_RE.findall(page)}


def page_title(page: str) -> str | None:
    match = TITLE_RE.search(page)
    return re.sub(r"\s+", " ", html_lib.unescape(TAG_RE.sub("", match.group(1)))).strip() if match else None


# --- field readers -----------------------------------------------------------------------------

def typology_from_text(*texts: str | None) -> int | None:
    """T from a title or description. `T1+1` -> 1: the compound rooms are not bedrooms."""
    for text in texts:
        if not text:
            continue
        match = TYPOLOGY_TEXT_RE.search(normalize_text(text))
        if match:
            base = int(match.group(1))
            # A bare two-digit T with no "+" in the text is a compound written without the sign (slug style)
            # or a transcription error; refuse to guess rather than invent a T21.
            if base > 10 and not match.group(2):
                continue
            return base
    return None


def typology_from_slug(url: str) -> int | None:
    match = TYPOLOGY_SLUG_RE.search(urlparse(url).path.lower())
    return int(match.group(1)) if match else None


def area_from_text(*texts: str | None) -> float | None:
    for text in texts:
        if not text:
            continue
        flat = normalize_text(text)
        near = AREA_NEAR_WORD_RE.search(flat)
        raw = (near.group(1) or near.group(2)) if near else (AREA_RE.search(flat).group(1) if AREA_RE.search(flat) else None)
        value = _number(raw) if raw else None
        if value and 10 <= value <= 20000:
            return value
    return None


def estate_from_text(*texts: str | None) -> str | None:
    flat = normalize_text(" ".join(t for t in texts if t))
    return next((kind for kind, pattern in ESTATE_FROM_TEXT if pattern.search(flat)), None)


def features_from_amenities(names: list[str]) -> dict[str, bool | None]:
    """Named amenities -> typed `confirmed` features.

    What is *not* done here matters: an amenity missing from the list is left `unknown`, never `absent`.
    Whether these lists are exhaustive has not been measured, and Rosa's criteria hide unknown must-haves,
    so inferring absence would silently drop real matches (`Plan_Day5.md` §9 item 4).
    """
    flat = [normalize_text(n) for n in names if n]
    typed: dict[str, bool | None] = {}
    for key, words in AMENITY_TO_FEATURE.items():
        if any(any(w in name for w in words) for name in flat):
            typed[key] = True
    return typed


def image_urls(node: dict, base_url: str, size_rewrite: list[str] | None) -> list[str]:
    raw = node.get("image") or node.get("primaryImageOfPage") or []
    items = raw if isinstance(raw, list) else [raw]
    urls: list[str] = []
    for item in items:
        url = item.get("url") or item.get("contentUrl") if isinstance(item, dict) else item
        if isinstance(url, str) and url.strip():
            urls.append(urljoin(base_url, url.strip()))
    if size_rewrite and len(size_rewrite) == 2:
        # pHash is size-stable (measured: 320x240 vs 1280x960 -> Hamming 0), so hashing the small variant is
        # a 10x bandwidth cut with the threshold-8 dedup rule intact.
        urls = [u.replace(size_rewrite[0], size_rewrite[1]) for u in urls]
    seen, unique = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _iso(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- the reader --------------------------------------------------------------------------------

ID_MAX = 120  # listings.external_id is String(120)


def external_id(url: str, profile: dict) -> str:
    """Stable per-source id for a listing URL.

    Falls back to the URL path when the profile has no usable pattern, but always within `ID_MAX`: an
    untruncated URL would overflow `listings.external_id` and fail the insert. A long path keeps a hash tail
    so two different listings can never collapse onto the same id.
    """
    index = (profile or {}).get("index") or {}
    pattern = index.get("listing_url_re")
    path = urlparse(url).path
    if pattern:
        try:
            match = re.search(pattern, path)
        except re.error:
            match = None
        if match:
            group = index.get("id_group") or 0
            try:
                found = match.group(group) if group else match.group(0)
            except IndexError:
                found = match.group(0)
            if found:
                return found.strip("/").replace("/", "-")[:ID_MAX]
    flat = path.strip("/").replace("/", "-") or url
    if len(flat) <= ID_MAX:
        return flat
    from hashlib import blake2s
    return f"{flat[:ID_MAX - 13]}-{blake2s(flat.encode('utf-8'), digest_size=6).hexdigest()}"


def extract(page: str, url: str, profile: dict, source_key: str, resolve=None) -> CanonicalListing | None:
    """One listing page -> canonical listing, using only what the page actually publishes.

    `resolve(text) -> (geo_code, level)` resolves the zone, following the same callback pattern as
    `idealista_email.to_canonical`: the parser stays free of database access and the caller supplies the geo
    lookup. Without it `geo_code` stays None and the zone text is kept in `raw` for a later pass.

    Returns None when the page carries no recognisable listing at all, which is what makes a site `none`
    rather than silently producing empty rows.
    """
    nodes = jsonld_nodes(page)
    listing = _first(nodes, "RealEstateListing", "Residence", "Apartment", "House", "SingleFamilyResidence",
                     "Product", "Offer", "Accommodation", "Place")
    meta = metas(page)
    title = (listing or {}).get("name") or meta.get("og:title") or page_title(page)
    description = (listing or {}).get("description") or meta.get("og:description") or meta.get("description")
    if not title and not description:
        return None

    # price + transaction: the Offer may be a sibling node (grupovantagem) or inline (eGO, portadafrente).
    offer = next((n for n in nodes if "offer" in _types(n) and n.get("price") is not None), None)
    if offer is None:
        inline = (listing or {}).get("offers")
        offer = inline if isinstance(inline, dict) else (inline[0] if isinstance(inline, list) and inline else None)
    offer = offer or {}
    price = _number(offer.get("price"))
    business = " ".join(str(offer.get(k, "")) for k in ("businessFunction", "@id", "url"))
    slug_text = urlparse(url).path.lower()
    transaction = "rent" if (RENT_MARKERS.search(business) or RENT_MARKERS.search(slug_text)) and not SELL_MARKERS.search(business) else "sale"

    # typed geometry where the platform publishes it, text where it does not (eGO)
    detail = _first(nodes, "House", "Apartment", "Residence", "SingleFamilyResidence", "Accommodation") or listing or {}
    # `derived` records which fields the page did NOT type and the reader had to read out of prose. The
    # profiler caps confidence at `partial` when a core field is only derived: a regex over a description is
    # genuinely less reliable than a typed field, and saying `full` would overstate it to her.
    derived: set[str] = set()
    rooms = detail.get("numberOfBedrooms") if detail.get("numberOfBedrooms") is not None else (listing or {}).get("numberOfBedrooms")
    if _number(rooms) is not None:
        rooms = int(_number(rooms))
    else:
        rooms = typology_from_text(title, description) or typology_from_slug(url)
        if rooms is not None:
            derived.add("typology")
    area = _number(detail.get("floorSize")) or _number((listing or {}).get("floorSize"))
    if area is None:
        area = area_from_text(description, title)
        if area is not None:
            derived.add("area")
    wc = _number(detail.get("numberOfBathroomsTotal"))

    address = detail.get("address") or (listing or {}).get("address") or {}
    address = address if isinstance(address, dict) else {}
    geo_node = detail.get("geo") or (listing or {}).get("geo") or {}
    geo_node = geo_node if isinstance(geo_node, dict) else {}
    # eGO publishes no address node at all: its only location is the title ("… em Alcântara - Lisboa"), so
    # the title is part of the zone text, not a fallback.
    zone_text = " ".join(str(address.get(k) or "") for k in
                         ("addressRegion", "addressLocality", "addressSubdivision", "streetAddress"))
    typed_zone = bool(zone_text.strip())
    zone_text = re.sub(r"\s+", " ", f"{zone_text} {title or ''}").strip()
    geo_code, geo_level = (resolve(zone_text) if resolve and zone_text else (None, None))
    if geo_code and not typed_zone:
        derived.add("location")

    agent = _first(nodes, "RealEstateAgent", "Organization") or {}
    provider = (listing or {}).get("provider") if isinstance((listing or {}).get("provider"), dict) else {}
    offered_by = offer.get("offeredBy") if isinstance(offer.get("offeredBy"), dict) else {}
    agency_name = agent.get("name") or provider.get("name") or offered_by.get("name")

    amenities = [a.get("name") for a in (detail.get("amenityFeature") or (listing or {}).get("amenityFeature") or [])
                 if isinstance(a, dict)]
    typed = features_from_amenities([a for a in amenities if a])
    parking = _number(detail.get("numberOfParkingSpaces"))
    if parking is not None:
        typed["garagem"] = parking > 0 or typed.get("garagem", False)
    features = merge_features(typed, features_from_text(title, description))
    if parking is not None and parking > 0:
        features["garagem"] = {"value": True, "tier": TIER_CONFIRMED}

    availability = str(offer.get("availability") or "")
    status = "sold" if re.search(r"soldout|outofstock|discontinued", availability, re.I) else "active"
    # Read the description too, not just the title: eGO's Hyatt Residences listing names the development
    # only in its body text. Being generous here is the safe direction - `criteria.include_new_developments`
    # defaults to True, so the flag costs nothing for filtering, while a missed development is a photo-merge
    # of two different units of the same building.
    is_development = bool(DEVELOPMENT_RE.search(normalize_text(f"{title or ''} {slug_text} {description or ''}")))

    return CanonicalListing(
        source_key=source_key,
        external_id=external_id(url, profile),
        url=url,
        title=title,
        price=int(price) if price else None,
        hide_price=price is None,
        area_util=area,
        rooms=rooms,
        wc=int(wc) if wc else None,
        estate_type=estate_from_text(title, description, slug_text),
        transaction=transaction,
        geo_code=geo_code,
        street=address.get("streetAddress") or None,
        lat=_number(geo_node.get("latitude")),
        lng=_number(geo_node.get("longitude")),
        agency_name=agency_name or None,
        agency_phone=agent.get("telephone") or provider.get("telephone") or None,
        is_new_development=is_development,
        features=features,
        images=image_urls(listing or detail, url, ((profile or {}).get("images") or {}).get("size_rewrite")),
        description=re.sub(r"\s+", " ", description).strip() if description else None,
        published_at=_iso((listing or detail).get("datePosted")),
        status=status,
        raw={"zone_text": zone_text, "zone_level": geo_level, "amenities": amenities,
             "derived_fields": sorted(derived),
             "date_modified": (listing or detail).get("dateModified"),
             "jsonld_types": sorted({t for n in nodes for t in _types(n)})},
    )


def derived_fields(canonical: CanonicalListing | None) -> set[str]:
    """Fields the reader had to pull out of prose because the page never typed them."""
    return set((canonical.raw or {}).get("derived_fields") or ()) if canonical else set()


def resolved_fields(canonical: CanonicalListing | None) -> set[str]:
    """Which of the five profiled fields this page actually yielded. The profiler turns this into the
    confidence level, so confidence is always a measurement of the reader, never a declaration."""
    if canonical is None:
        return set()
    found = set()
    if canonical.title:
        found.add("title")
    if canonical.price is not None:
        found.add("price")
    if canonical.rooms is not None:
        found.add("typology")
    if canonical.area_util or canonical.area_bruta:
        found.add("area")
    # Location counts only when it really resolved to a geo code (or to coordinates). Counting "there was a
    # title" here would inflate every site to `full`, which is exactly the silent-zero the gate forbids.
    if canonical.geo_code or (canonical.lat and canonical.lng):
        found.add("location")
    if canonical.published_at:
        found.add("published")
    if canonical.features:
        found.add("features")
    return found


class TransientFetchError(RuntimeError):
    """The page could not be read. Not the same as the listing being withdrawn.

    `services.refresh.refresh_matched` reads a returned None as "gone" and marks the listing removed, but
    counts an exception as a failure and moves on. So an unreadable page must raise, never return None."""


class AgencyAdapter:
    """Refresh support for agency listings, so Day 4's `matched_refresh` covers them too.

    Agency sites are swept by `services.agencies.watch`, not by `runs.execute_sweep`, so this class
    deliberately implements only `refresh_listing`. Without it a price change on a listing an eGO site had
    already published would be invisible: eGO publishes no `<lastmod>`, so the set-difference pass only ever
    fetches ids it has not seen before. Re-reading the matched ones daily closes that gap with code that is
    already proven, instead of a second mechanism.
    """

    scope_mode = "catalogue"

    def __init__(self, source) -> None:
        self.source = source
        self.key = source.key

    def refresh_listing(self, session, listing) -> CanonicalListing | None:
        from app.geo import resolve as geo_resolve
        from app.sources.http import get_client

        try:
            response = get_client().get(listing.url, delay_s=self.source.crawl_delay_s, retries=1)
        except Exception as exc:  # noqa: BLE001 - a network blip must not retire a live listing
            raise TransientFetchError(f"{listing.url} não respondeu: {exc!r}") from exc
        if response.status_code in GONE_STATUS:
            return None
        if response.status_code != 200:
            raise TransientFetchError(f"{listing.url} devolveu HTTP {response.status_code}")
        canonical = extract(response.text, listing.url, self.source.profile or {}, self.source.key,
                            resolve=lambda text: geo_resolve.from_text(session, text))
        if canonical is None:
            raise TransientFetchError(f"{listing.url} já não expõe um imóvel legível")
        return canonical
