"""RE/MAX adapter (Milestone1_Plan.md §1.2, Roadmap_M1.md §7).

Contract verified on live requests 2026-10-30:
- robots: `Allow: /`, `Disallow: /api /static /myprofile /perfil /mylisting`. We use the sitemap and the
  public detail pages only; the internal API is never touched.
- `www.remax.pt` 301-redirects to `remax.pt`. Canonical host is `remax.pt`.
- sitemap index `/sitemap.xml` -> `listings_details_pt_{1..4}.xml.gz`, 100 000 `<loc>` entries. Each listing
  appears twice: bare `/pt/<id>` and slugged `/pt/imoveis/<slug>/<id>`. 50 000 real listings; 99 999 carry
  `<lastmod>` (one does not).
- the `.xml.gz` files are served with `Content-Encoding: gzip`, so an HTTP client that negotiates encoding
  receives plain XML. Sniff the gzip magic; never assume one or the other.
- the slug encodes transaction + type + typology + concelho + freguesia, so the working set is selected
  before a single detail page is fetched: 12 313 of 50 000 are in her three districts, 10 138 of those are
  `venda` + apartamento/moradia. 17 130 slugs have an empty typology (`venda-terreno-t<concelho>`); all are
  non-residential, so `t(\\d*)` must not raise on them. Zero apartamento/moradia in her districts lack a `t<n>`.
- detail `/pt/imoveis/<slug>/<id>` -> `"listingEncoded":"<base64>"` -> JSON, 153 fields (8/8 pages).

Field traps found on nine live listings (see tests/fixtures/remax_listings.json):
- `typologyID = 2 x numberOfBedrooms + 1` (3->T1, 5->T2, 7->T3, 9->T4, 11->T5). Use `numberOfBedrooms`.
- `previousPrice` has no direction: one of the nine is a price *rise* (393 000 -> 410 000).
  `lastPriceReductionDate` is non-null on exactly the reductions. Never treat `previousPrice` alone as a drop.
- `isSold` is True while `isActive` is also True, and the listing stays in the sitemap. `isSold` wins.
- `officePhoneNumber` is null on 8/8; `userAgencyPhoneNumber` carries the office switchboard. Without the
  fallback, deliverable 12 ("mediadora + contacto") would ship empty for this source.
- `listingTitle` and `autocompleteListingTitle` are just the id; the non-PT `descriptions[]` entries are
  machine-generated and sometimes state the wrong typology. The title is built from the slug.
- `listingReference` is null on 9/9, so the dedup reference rule never fires here; cross-source merging with
  Imovirtual relies on the photo rule.
- `livingArea` 8/8, `builtArea` 2/8, `lotSize` 1/8. `totalArea` is not used: on one listing livingArea 393 =
  totalArea 393 while builtArea is 653, so its meaning is not stable.
- `floorAsNumber` is the only unambiguous floor field (0 = rés-do-chão). Not cross-checked against a page
  that states its floor in text; it feeds only the dedup tie-breaker, where a wrong value can block a merge
  (two entries shown separately) but never cause a false one.
- `publishDate` is naive (no offset); it is read as UTC. The worst error is one hour, which no rule depends on.

GDPR (plan §7): individual-agent fields are dropped at the parser boundary, before `raw` is stored, so they
never reach the database. Office and agency contact data is business data and is kept.
"""
from __future__ import annotations

import gzip
import html as html_mod
import json
import logging
import re
from base64 import b64decode
from collections.abc import Iterator
from datetime import datetime, timezone

from app.domain.listing import CanonicalListing, features_from_text, merge_features
from app.geo.resolve import slugify
from app.sources.http import get_client
from app.sources.registry import register

log = logging.getLogger(__name__)

BASE = "https://remax.pt"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
IMAGE_BASE = "https://i.maxwork.pt/ds-l/"

LISTING_SITEMAP_RE = re.compile(r"/listings_details_pt_\d+\.xml(?:\.gz)?$")
URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S)
SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
# Slugged detail URL only; the bare /pt/<id> twin carries no slug and is skipped.
SLUG_URL_RE = re.compile(r"^https?://(?:www\.)?remax\.pt/pt/imoveis/([a-z0-9\-]+)/(\d+-\d+)/?$")
SLUG_HEAD_RE = re.compile(r"^(?P<transaction>venda|arrendamento|trespasse)-(?P<estate>[a-z0-9]+)-t(?P<t>\d*)-(?P<rest>.+)$")
TAG_RE = re.compile(r"<[^>]+>")

TRANSACTION = {"venda": "sale", "arrendamento": "rent"}
# duplex and estudio are flats in Portuguese practice; mapping them keeps ~200 national listings reachable
# by a criteria that asks for "apartamento". `quinta` is deliberately NOT mapped to moradia: that would widen
# her result set on a judgement call, so it stays an explicit decision to offer her.
ESTATE = {"apartamento": "apartamento", "duplex": "apartamento", "estudio": "apartamento", "moradia": "moradia"}
RESIDENTIAL = ("apartamento", "moradia")

# Individual-agent fields. Dropped before the payload is stored or read (plan §7).
PII_FIELDS = frozenset({
    "userEmail", "userCellPhone", "userCellPhoneTrimmed", "userName", "userPublicName", "userPictureURL",
    "userID", "agentID", "teamID", "userTeamName", "whatsAppChatLink", "whatsAppChatIntroId",
    "hasWhatsAppChatLink", "ownerAvailability", "hasOwnerAvailability", "ownerPageId",
})
# Kept on purpose: officeName / officePhoneNumber / officeCellPhone / officeAddress and the userAgency*
# fields, which identify the agency as a business (the AMI licence number is public registry data).


# --- sitemap ------------------------------------------------------------------------------------

def maybe_gunzip(body: bytes) -> bytes:
    """The `.xml.gz` files arrive decompressed when the client negotiates `Content-Encoding: gzip`."""
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def listing_sitemaps(index_xml: str) -> list[str]:
    return [loc for loc in SITEMAP_LOC_RE.findall(index_xml) if LISTING_SITEMAP_RE.search(loc)]


def iter_entries(xml: str) -> Iterator[tuple[str, str | None]]:
    """(loc, lastmod) for every <url> block. `lastmod` is missing on one entry of the real sitemap."""
    for block in URL_BLOCK_RE.finditer(xml):
        body = block.group(1)
        loc = LOC_RE.search(body)
        if not loc:
            continue
        mod = LASTMOD_RE.search(body)
        yield loc.group(1), (mod.group(1) if mod else None)


class SlugInfo:
    __slots__ = ("transaction", "estate_type", "rooms", "council_code", "parish_slug", "slug")

    def __init__(self, transaction, estate_type, rooms, council_code, parish_slug, slug):
        self.transaction, self.estate_type, self.rooms = transaction, estate_type, rooms
        self.council_code, self.parish_slug, self.slug = council_code, parish_slug, slug


def parse_slug(slug: str, councils: dict[str, str]) -> SlugInfo | None:
    """`venda-apartamento-t2-vila-franca-de-xira-vialonga` -> typed fields + `lisboa/vila-franca-de-xira`.

    `councils` maps a council slug to its full geo code. The match is anchored and longest-first, so
    `vila-franca-de-xira` wins over any shorter council slug that happens to be a prefix."""
    head = SLUG_HEAD_RE.match(slug)
    if not head:
        return None
    transaction = TRANSACTION.get(head.group("transaction"))
    estate = ESTATE.get(head.group("estate"))
    if transaction is None or estate is None:
        return None
    rest = head.group("rest")
    for council_slug in councils:  # pre-sorted longest-first by `council_index`
        if rest == council_slug or rest.startswith(council_slug + "-"):
            parish = rest[len(council_slug):].lstrip("-") or None
            rooms = int(head.group("t")) if head.group("t") else None
            return SlugInfo(transaction, estate, rooms, councils[council_slug], parish, slug)
    return None


def council_index(session) -> dict[str, str]:
    """{council slug: geo code}, longest slug first so `parse_slug` anchors correctly."""
    from sqlalchemy import select

    from app.db.models import Geo

    rows = session.scalars(select(Geo).where(Geo.level == "council"))
    pairs = sorted(((g.code.split("/")[-1], g.code) for g in rows), key=lambda p: len(p[0]), reverse=True)
    return dict(pairs)


def _parse_mod(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def select_refs(entries: Iterator[tuple[str, str | None]], councils: dict[str, str],
                since: datetime | None) -> list[tuple[str, SlugInfo, datetime | None]]:
    """Slug pre-filter: the single biggest efficiency lever. Keeps `venda` + residential in her councils,
    whose catalogue entry changed after `since`. An entry with no `lastmod` is always kept (it cannot be
    proven unchanged)."""
    out: list[tuple[str, SlugInfo, datetime | None]] = []
    for loc, mod in entries:
        match = SLUG_URL_RE.match(loc)
        if not match:
            continue  # the bare /pt/<id> twin, or a non-listing page
        info = parse_slug(match.group(1), councils)
        if info is None or info.transaction != "sale" or info.estate_type not in RESIDENTIAL:
            continue
        when = _parse_mod(mod)
        if since is not None and when is not None and when <= since:
            continue
        out.append((loc, info, when))
    return out


# --- detail -------------------------------------------------------------------------------------

ENCODED_RE = re.compile(r'"listingEncoded"\s*:\s*"([A-Za-z0-9+/=]{100,})"')


def extract_listing_encoded(page_html: str) -> dict | None:
    match = ENCODED_RE.search(page_html)
    if not match:
        return None
    try:
        return json.loads(b64decode(match.group(1)))
    except (ValueError, json.JSONDecodeError):
        log.warning("remax: listingEncoded present but did not decode (parser drift?)")
        return None


def strip_pii(payload: dict) -> dict:
    """Drop individual-agent fields before anything else sees the payload (plan §7)."""
    return {k: v for k, v in payload.items() if k not in PII_FIELDS}


def _pt_description(payload: dict) -> str | None:
    for entry in payload.get("descriptions") or []:
        if isinstance(entry, dict) and (entry.get("languageCode") or "").upper() == "PT":
            text = html_mod.unescape(TAG_RE.sub(" ", entry.get("description") or ""))
            return re.sub(r"\s+", " ", text).strip() or None
    return None


def _parse_publish(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status(payload: dict) -> str:
    if payload.get("isSold"):
        return "sold"  # isActive is True on sold listings too, and they stay in the sitemap
    if payload.get("isActive") is False or payload.get("isOnline") is False or payload.get("isPublished") is False:
        return "removed"
    return "active"


def _exterior(payload: dict) -> bool | None:
    """Rosa's obligatory outdoor space, as a typed field. None when the source says nothing; an explicit 0
    means the listing states it has none."""
    values = [payload.get("lotSize"), payload.get("exteriorPrivateArea")]
    known = [v for v in values if v is not None]
    if not known:
        return None
    return any(float(v) > 0 for v in known)


def _typed_features(payload: dict) -> dict[str, bool | None]:
    garage = payload.get("garage")
    if garage is None and payload.get("garageSpots"):
        garage = True
    return {"elevador": payload.get("elevator"), "garagem": garage, "exterior": _exterior(payload)}


def _title(info: SlugInfo, payload: dict) -> str:
    where = ", ".join(filter(None, [payload.get("regionName2"), payload.get("regionName3") or payload.get("localZone")]))
    typology = f" T{info.rooms}" if info.rooms is not None else ""
    return f"{info.estate_type.capitalize()}{typology}" + (f" — {where}" if where else "")


def _geo(info: SlugInfo, payload: dict) -> tuple[str | None, list[tuple[str, str, str]]]:
    """District and council come from the slug (that is what we filtered on and it always matches the seed);
    only the parish is taken from the payload. A payload whose district contradicts the slug is a same-named
    council in another district and is rejected by the caller."""
    district_code, council_slug = info.council_code.split("/", 1)
    path = [(district_code, "district", payload.get("regionName1") or district_code.capitalize()),
            (info.council_code, "council", payload.get("regionName2") or council_slug.replace("-", " ").title())]
    parish_name = payload.get("regionName3")
    parish_slug = slugify(parish_name) if parish_name else info.parish_slug
    if parish_slug:
        code = f"{info.council_code}/{parish_slug}"
        path.append((code, "parish", parish_name or parish_slug.replace("-", " ").title()))
    return path[-1][0], path


def council_matches_payload(info: SlugInfo, payload: dict) -> bool:
    """Guard against a council whose name exists in another district: the payload's own district must agree
    with the one the slug resolved to."""
    district = payload.get("regionName1")
    return not district or slugify(district) == info.council_code.split("/", 1)[0]


def normalise(payload: dict, url: str, info: SlugInfo) -> CanonicalListing:
    """`listingEncoded` (already PII-stripped) -> CanonicalListing."""
    match = SLUG_URL_RE.match(url)
    if match is None:
        raise ValueError(f"remax: {url} is not a slugged detail URL")
    external_id = match.group(2)
    description = _pt_description(payload)
    price = payload.get("listingPrice")
    previous = payload.get("previousPrice")
    reduced_at = _parse_publish(payload.get("lastPriceReductionDate"))
    geo_code, geo_path = _geo(info, payload)
    bedrooms = payload.get("numberOfBedrooms")  # never typologyID: it is 2*bedrooms+1
    images = [IMAGE_BASE + p.lstrip("/") for p in (payload.get("listingPictures") or []) if isinstance(p, str)]
    return CanonicalListing(
        source_key="remax",
        external_id=external_id,
        url=url,
        title=_title(info, payload),
        price=int(price) if price is not None else None,
        hide_price=False,
        area_util=float(payload["livingArea"]) if payload.get("livingArea") is not None else None,
        area_bruta=float(payload["builtArea"]) if payload.get("builtArea") is not None else None,
        land_area=float(payload["lotSize"]) if payload.get("lotSize") is not None else None,
        rooms=int(bedrooms) if bedrooms is not None else info.rooms,
        wc=payload.get("numberOfWC") or payload.get("numberOfBathrooms"),
        floor=payload.get("floorAsNumber"),
        estate_type=info.estate_type,
        transaction=info.transaction,
        geo_code=geo_code,
        geo_path=geo_path,
        street=payload.get("publicAddress") or None,
        lat=payload.get("latitude"),
        lng=payload.get("longitude"),
        agency_name=payload.get("officeName"),
        agency_phone=payload.get("officePhoneNumber") or payload.get("userAgencyPhoneNumber"),
        reference=payload.get("listingReference") or None,
        is_private_owner=False,  # every RE/MAX listing belongs to a franchise office
        is_new_development=bool(payload.get("buildingDevelopmentName") or payload.get("buildingDevelopmentID")),
        features=merge_features(_typed_features(payload), features_from_text(description)),
        images=images[:10],
        description=description,
        published_at=_parse_publish(payload.get("publishDate")),
        previous_price=int(previous) if previous is not None else None,
        price_reduced_at=reduced_at if (previous is not None and price is not None and previous > price) else None,
        status=_status(payload),
        raw=payload,
    )


# --- adapter ------------------------------------------------------------------------------------

@register("remax")
class RemaxAdapter:
    key = "remax"
    scope_mode = "catalogue"  # one global sitemap; the slug does the filtering. See sources/base.py

    def __init__(self, source) -> None:
        self.source = source
        self.client = get_client()
        self.delay = getattr(source, "crawl_delay_s", 1.5) or 1.5
        self._councils: dict[str, str] | None = None

    # -- fetching
    def _get(self, url: str):
        from app.sources.robots import is_allowed

        if not is_allowed(self.source, url):
            raise PermissionError(f"robots.txt disallows {url}")
        return self.client.get(url, delay_s=self.delay)

    def councils(self, session) -> dict[str, str]:
        if self._councils is None:
            self._councils = council_index(session)
        return self._councils

    def fetch_refs(self, session, since: datetime | None) -> list[tuple[str, SlugInfo, datetime | None]]:
        """Read the four listing sitemaps and return the filtered working set, in catalogue order."""
        response = self._get(SITEMAP_INDEX)
        response.raise_for_status()
        sitemaps = listing_sitemaps(response.text)
        if not sitemaps:
            raise RuntimeError("remax: sitemap index has no listings_details_pt_* files (parser drift?)")
        councils = self.councils(session)
        refs: list[tuple[str, SlugInfo, datetime | None]] = []
        for url in sitemaps:
            page = self._get(url)
            page.raise_for_status()
            xml = maybe_gunzip(page.content).decode("utf-8", "replace")
            refs.extend(select_refs(iter_entries(xml), councils, since))
        log.info("remax: %d sitemaps -> %d refs after the slug filter", len(sitemaps), len(refs),
                 extra={"source": self.key})
        return refs

    def fetch_listing(self, url: str, info: SlugInfo) -> CanonicalListing | None:
        response = self._get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = extract_listing_encoded(response.text)
        if payload is None:
            raise RuntimeError(f"remax: no listingEncoded on {url} (parser drift?)")
        payload = strip_pii(payload)
        if not council_matches_payload(info, payload):
            log.debug("remax: %s resolved to %s but the payload says %s; skipped",
                      url, info.council_code, payload.get("regionName1"))
            return None
        return normalise(payload, url, info)

    def refresh_listing(self, session, listing) -> CanonicalListing | None:
        """Re-read one stored listing. For RE/MAX the detail page is the only page there is, so the refresh
        and the first read go through exactly the same code. None means gone (404)."""
        info = parse_slug(SLUG_URL_RE.match(listing.url).group(1), self.councils(session)) if SLUG_URL_RE.match(listing.url) else None
        if info is None:
            return None
        return self.fetch_listing(listing.url, info)

    # -- the catalogue contract
    def iter_changed(self, since: datetime | None, *, limit: int | None = None,
                     cursor: dict | None = None, session=None) -> Iterator[CanonicalListing]:
        """Listings whose sitemap entry changed after `since`, in catalogue order.

        `cursor` (a dict the caller persists on `crawl_runs.cursor`) makes the pass resumable: it holds the
        position reached and the URL at that position, so a restart re-reads the four sitemaps, finds its
        place again and continues. `limit` bounds one slice, which is how the backfill is measured before
        the whole working set is committed to."""
        if session is None:
            raise ValueError("remax.iter_changed needs a session to resolve councils")
        refs = self.fetch_refs(session, since)
        start = _resume_at(refs, cursor)
        stop = len(refs) if limit is None else min(len(refs), start + limit)
        if cursor is not None:
            cursor["total"] = len(refs)
        for index in range(start, stop):
            url, info, _ = refs[index]
            try:
                listing = self.fetch_listing(url, info)
            except PermissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad page must not end a 10 000-page pass
                log.warning("remax: %s failed: %r", url, exc, extra={"source": self.key})
                listing = None
            if cursor is not None:
                cursor["index"], cursor["last_url"] = index + 1, url
            if listing is not None:
                yield listing


def _resume_at(refs: list[tuple[str, SlugInfo, datetime | None]], cursor: dict | None) -> int:
    """Where to continue. Prefers the recorded URL over the recorded index, because the catalogue can shift
    by a few entries between runs."""
    if not cursor:
        return 0
    index = int(cursor.get("index") or 0)
    last_url = cursor.get("last_url")
    if last_url:
        if 0 < index <= len(refs) and refs[index - 1][0] == last_url:
            return index
        for position, (url, _, _) in enumerate(refs):
            if url == last_url:
                return position + 1
    return min(index, len(refs))
