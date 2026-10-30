"""Imovirtual adapter (Milestone1_Plan.md §1.1, Roadmap_M1.md §7).

Contract verified 2026-10-27 / 2026-10-28:
- search: /pt/resultados/comprar/<apartamento|moradia>/<distrito>/<concelho>?by=LATEST&direction=DESC&page=N
  (+ priceMin / priceMax). JSON in <script id="__NEXT_DATA__"> at props.pageProps.data.searchAds.{items,pagination}
- detail: /pt/anuncio/<slug> -> props.pageProps.ad
- `roomsNumber` counts DIVISIONS, not bedrooms (slug T2 -> "THREE"), so typology = roomsNumber - 1. The detail page's
  `Rooms_num` / `rooms_num` follow the same convention. Verified on a live page (26/26) and 1 268 stored ads.
- `floorNumber` is an enum (CELLAR, GROUND, FIRST ... TENTH, ABOVE_TENTH); null on most houses.
- robots Content-Signal ai-input=no: nothing from this source is ever sent to an LLM. Extraction is rules only.
"""
from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

from app.domain.listing import CanonicalListing, features_from_text, merge_features, normalize_text
from app.sources.http import get_client
from app.sources.registry import register

log = logging.getLogger(__name__)

BASE = "https://www.imovirtual.com"
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# Number of divisions as Imovirtual sends it; typology T = divisions - 1 (see module docstring). MORE = more than ten.
DIVISIONS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10, "MORE": 11}
FLOORS = {"CELLAR": -1, "GROUND": 0, "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7,
          "EIGHTH": 8, "NINTH": 9, "TENTH": 10, "ABOVE_TENTH": 11}
ESTATE = {"FLAT": "apartamento", "HOUSE": "moradia", "TERRAIN": "terreno"}
TRANSACTION = {"SELL": "sale", "RENT": "rent"}
GEO_LEVELS = ("district", "council", "parish")  # neighbourhood is dropped: our taxonomy stops at parish
# Typed feature labels (Imovirtual sends English or Portuguese keys depending on the feed) -> our feature keys
TYPED_FEATURES = {
    "garagem": ("garage", "garagem", "parking", "estacionamento", "box"),
    "elevador": ("lift", "elevator", "elevador"),
    "varanda": ("balcony", "varanda"),
    "terraco": ("terrace", "terraco"),
    "jardim": ("garden", "jardim"),
    "piscina": ("pool", "swimming_pool", "piscina"),
    "arrecadacao": ("storage", "storeroom", "arrecadacao"),
    "exterior": ("garden", "terrace", "jardim", "terraco", "quintal", "logradouro", "terreno", "patio"),
}
TAG_RE = re.compile(r"<[^>]+>")


def typology_from_divisions(value) -> int | None:
    """Imovirtual division count (enum string or number) -> Portuguese typology T. None when absent."""
    if value is None or value == "":
        return None
    divisions = DIVISIONS.get(value) if isinstance(value, str) and not value.isdigit() else int(value)
    if divisions is None:
        return None
    return max(divisions - 1, 0)


def extract_next_data(html: str) -> dict | None:
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        log.warning("imovirtual: __NEXT_DATA__ present but not valid JSON")
        return None


def search_url(estate: str, district: str, council: str, page: int = 1, price_min: int | None = None, price_max: int | None = None) -> str:
    params = [("by", "LATEST"), ("direction", "DESC"), ("page", str(page))]
    if price_min is not None:
        params.append(("priceMin", str(price_min)))
    if price_max is not None:
        params.append(("priceMax", str(price_max)))
    query = "&".join(f"{k}={v}" for k, v in params)
    return f"{BASE}/pt/resultados/comprar/{estate}/{district}/{council}?{query}"


def detail_url(slug: str) -> str:
    return f"{BASE}/pt/anuncio/{slug}"


def public_id(slug: str | None) -> str | None:
    """Stable listing key: the token after "-ID" in the slug (e.g. "moradia-t2-para-venda-ID1iZ6K" -> "1iZ6K").

    Why not the numeric `id`: Imovirtual injects promoted tiles with a fixed synthetic id (9000062259, 9000062261, ...)
    whose content rotates on every page; keyed by that id they would overwrite each other and fake price changes
    (seen 2026-10-28: 71 bogus price rows in one evening). The slug token identifies the real listing, and survives
    title edits that change the rest of the slug."""
    if slug and "-ID" in slug:
        token = slug.rsplit("-ID", 1)[1].strip()
        return token or None
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _geo(location: dict | None) -> tuple[str | None, list[tuple[str, str, str]]]:
    path: list[tuple[str, str, str]] = []
    for loc in ((location or {}).get("reverseGeocoding") or {}).get("locations") or []:
        level = loc.get("locationLevel")
        if level in GEO_LEVELS and loc.get("id") and loc.get("name"):
            path.append((loc["id"], level, loc["name"]))
    path.sort(key=lambda p: GEO_LEVELS.index(p[1]))
    return (path[-1][0] if path else None), path


def parse_search(data: dict) -> tuple[list[dict], dict]:
    search_ads = (((data or {}).get("props") or {}).get("pageProps") or {}).get("data", {}).get("searchAds") or {}
    return list(search_ads.get("items") or []), dict(search_ads.get("pagination") or {})


def normalise_item(item: dict, default_estate: str | None = None) -> CanonicalListing:
    price_obj = item.get("totalPrice") or {}
    estate = ESTATE.get(item.get("estate") or "", None) or default_estate
    geo_code, geo_path = _geo(item.get("location"))
    street = (((item.get("location") or {}).get("address") or {}).get("street") or {}).get("name")
    agency = item.get("agency") or {}
    owner = item.get("advertOwner") or {}
    images = [img.get("small") or img.get("medium") for img in (item.get("images") or []) if isinstance(img, dict)]
    title = item.get("title")
    short = item.get("shortDescription")
    return CanonicalListing(
        source_key="imovirtual",
        external_id=public_id(item.get("slug")) or str(item["id"]),
        url=detail_url(item["slug"]),
        title=title,
        price=int(price_obj["value"]) if price_obj.get("value") is not None else None,
        currency=price_obj.get("currency") or "EUR",
        hide_price=bool(item.get("hidePrice")),
        area_util=float(item["areaInSquareMeters"]) if item.get("areaInSquareMeters") is not None else None,
        land_area=float(item["terrainAreaInSquareMeters"]) if item.get("terrainAreaInSquareMeters") is not None else None,
        rooms=typology_from_divisions(item.get("roomsNumber")),
        floor=FLOORS.get(item.get("floorNumber") or ""),
        estate_type=estate,
        transaction=TRANSACTION.get(item.get("transaction") or "SELL", "sale"),
        geo_code=geo_code,
        geo_path=geo_path,
        street=street,
        agency_name=agency.get("name") or owner.get("name"),
        agency_slug=agency.get("slug"),
        agency_urn=item.get("source"),
        is_private_owner=bool(item.get("isPrivateOwner")),
        is_new_development=(item.get("estate") == "INVESTMENT") or bool(item.get("developmentId")),
        features=_features(title, short, land_area=item.get("terrainAreaInSquareMeters")),
        images=[u for u in images if u][:10],
        description=short,
        published_at=_parse_dt(item.get("dateCreated")) or _parse_dt(item.get("createdAtFirst")),
        published_unknown=bool(item.get("dateCreated")) and _parse_dt(item.get("dateCreated")) is None,  # "1999-02-29 00:00:01" on ~3 % of ads
        raw=item,
    )


def _features(title: str | None, description: str | None, *, land_area=None, typed: list[str] | None = None) -> dict:
    text_hits = features_from_text(title, description)
    typed_map: dict[str, bool | None] = {}
    for raw in typed or []:
        norm = normalize_text(raw)
        for key, synonyms in TYPED_FEATURES.items():
            if any(s in norm for s in synonyms):
                typed_map[key] = True
    if land_area:
        typed_map["exterior"] = True  # a stated terrain area is a typed confirmation of outdoor space
    return merge_features(typed_map, text_hits)


def _typed_feature_strings(ad: dict) -> list[str]:
    out: list[str] = []
    for key in ("features", "featuresWithoutCategory"):
        for entry in ad.get(key) or []:
            out.append(entry if isinstance(entry, str) else str(entry.get("label") or entry.get("value") or ""))
    for cat in ad.get("featuresByCategory") or []:
        for value in (cat.get("values") or []) if isinstance(cat, dict) else []:
            out.append(str(value))
    return [s for s in out if s]


def _truthy(value) -> bool:
    """Imovirtual mixes booleans with strings like "0" / "1" / "false" in the tracking `target` block."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def parse_detail(data: dict) -> dict | None:
    """Subset of the detail page used by matched_refresh: status, price, rooms, extras, phones, coordinates."""
    ad = (((data or {}).get("props") or {}).get("pageProps") or {}).get("ad")
    if not ad:
        return None
    target = ad.get("target") or {}
    chars = {c.get("key"): c.get("value") for c in ad.get("characteristics") or [] if isinstance(c, dict)}
    extra = {e.get("label"): (e.get("values") or [None])[0] for e in ad.get("additionalInformation") or [] if isinstance(e, dict)}
    geo_code, geo_path = _geo(ad.get("location"))
    coords = (ad.get("location") or {}).get("coordinates") or {}
    agency = ad.get("agency") or ad.get("owner") or {}
    rooms_raw = (target.get("Rooms_num") or [None])[0] or chars.get("rooms_num")
    active = ad.get("status") == "active" and not ad.get("shouldShowExpiredAdPage")
    return {
        "status": "active" if active else "removed",
        "price": int(target["Price"]) if target.get("Price") not in (None, "") else (int(float(chars["price"])) if chars.get("price") else None),
        "hide_price": _truthy(target.get("hidePrice")) or (ad.get("price") or {}).get("type") == "HIDDEN",
        "rooms": typology_from_divisions(rooms_raw),
        "wc": int(extra["bathrooms_num"]) if extra.get("bathrooms_num") else None,
        "build_year": int(extra["build_year"]) if extra.get("build_year") else None,
        "area_util": float(chars["m"]) if chars.get("m") else None,
        "reference": ad.get("referenceId") or ad.get("externalId"),
        "agency_phone": (agency.get("phones") or [None])[0],
        "agency_name": agency.get("name"),
        "agency_slug": agency.get("slug"),
        "lat": coords.get("latitude"),
        "lng": coords.get("longitude"),
        "geo_code": geo_code,
        "geo_path": geo_path,
        "description": re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", ad.get("description") or ""))).strip() or None,
        "typed_features": _typed_feature_strings(ad),
        "images": [img.get("small") or img.get("medium") for img in ad.get("images") or [] if isinstance(img, dict)][:10],
        "modified_at": _parse_dt(ad.get("modifiedAt")),
    }


def apply_detail(listing: CanonicalListing, detail: dict) -> CanonicalListing:
    """Enrich a listing with detail-page facts (used by matched_refresh)."""
    for key in ("status", "price", "hide_price", "rooms", "wc", "area_util", "reference", "agency_phone", "lat", "lng", "description"):
        if detail.get(key) is not None:
            setattr(listing, key, detail[key])
    if detail.get("geo_code"):
        listing.geo_code, listing.geo_path = detail["geo_code"], detail["geo_path"]
    if detail.get("images"):
        listing.images = detail["images"]
    listing.features = _features(listing.title, detail.get("description") or listing.description,
                                 land_area=listing.land_area, typed=detail.get("typed_features"))
    return listing


def paginate_newest(fetch_page: Callable[[int], tuple[list[CanonicalListing], dict]], since: datetime | None,
                    max_pages: int = 25, grace: timedelta = timedelta(minutes=10)) -> Iterator[CanonicalListing]:
    """Newest-first pagination with watermark stop. `since=None` means a full pass (initial run).
    Stops when the oldest item on a page is older than `since - grace`, when the last page is reached,
    or after `max_pages` (logged, so a stalled watermark is visible)."""
    cutoff = (since - grace) if since else None
    page = 1
    while page <= max_pages:
        items, pagination = fetch_page(page)
        if not items:
            return
        oldest: datetime | None = None
        for listing in items:
            if cutoff is None or listing.published_at is None or listing.published_at >= cutoff:
                yield listing
            if listing.published_at and (oldest is None or listing.published_at < oldest):
                oldest = listing.published_at
        if cutoff is not None and oldest is not None and oldest < cutoff:
            return
        if page >= int(pagination.get("totalPages") or 1):
            return
        page += 1
    log.warning("imovirtual: stopped at max_pages=%d without reaching the watermark", max_pages)


@register("imovirtual")
class ImovirtualAdapter:
    key = "imovirtual"

    def __init__(self, source) -> None:
        self.source = source
        self.client = get_client()
        self.delay = getattr(source, "crawl_delay_s", 2.0) or 2.0

    def _get(self, url: str):
        from app.sources.robots import is_allowed

        if not is_allowed(self.source, url):
            raise PermissionError(f"robots.txt disallows {url}")
        return self.client.get(url, delay_s=self.delay)

    def fetch_search_page(self, estate: str, district: str, council: str, page: int,
                          price_min: int | None = None, price_max: int | None = None) -> tuple[list[CanonicalListing], dict]:
        url = search_url(estate, district, council, page, price_min, price_max)
        response = self._get(url)
        response.raise_for_status()
        data = extract_next_data(response.text)
        if data is None:
            raise RuntimeError(f"imovirtual: no __NEXT_DATA__ on {url} (parser drift?)")
        items, pagination = parse_search(data)
        return [normalise_item(item, default_estate=estate) for item in items], pagination

    def iter_newest(self, estate: str, district: str, council: str, since: datetime | None) -> Iterator[CanonicalListing]:
        return paginate_newest(lambda page: self.fetch_search_page(estate, district, council, page), since)

    def iter_initial(self, estate: str, district: str, council: str, price_min: int | None, price_max: int | None) -> Iterator[CanonicalListing]:
        return paginate_newest(lambda page: self.fetch_search_page(estate, district, council, page, price_min, price_max), since=None, max_pages=60)

    def fetch_detail(self, slug_or_url: str) -> dict | None:
        url = slug_or_url if slug_or_url.startswith("http") else detail_url(slug_or_url)
        response = self._get(url)
        if response.status_code == 404:
            return {"status": "removed"}
        response.raise_for_status()
        data = extract_next_data(response.text)
        return parse_detail(data) if data else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
