"""Builds the declarative profile stored in `sources.profile`, and the only writer of it.

The cascade is ordered cheapest-first and stops as soon as the page yields enough; the LLM is the last
resort, not the first tool (`Milestone1/Plan_Day5.md` §3.2):

  1. robots.txt        1 req   allowed? Content-Signal? **platform fingerprint** - eGO is identified here,
                               with no page fetch at all, because its robots.txt is byte-identical across
                               sites (`/virtual/PrintProperty.aspx`, `SmartLink.aspx`, `gerir-dados-rgpd`).
  2. sitemap / index   1-2 req index URLs, pt-pt locale pick, and whether <lastmod> exists -> incremental mode
  3. listing-URL regex 0 req   derived from the URL shapes actually present in the sitemap
  4. 3 sample pages    3 req   run the real reader over them
  5. graph/OG/regex    0 req   already inside `agency_generic.extract`
  6. LLM profiler              only when fewer than 3 of {price, typology, location, title} resolved

Confidence is a **measurement of the reader**, never a declaration: it is the share of
{title, price, typology, location, area} that `agency_generic.extract` actually returned over the samples.
That is why the profiler imports the reader rather than reimplementing extraction - the two cannot drift.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from app.sources import agency_generic as reader
from app.sources import robots as robots_mod
from app.sources.http import HttpClient, get_client

log = logging.getLogger(__name__)

PLATFORM_EGO = "ego"
PLATFORM_JSONLD = "jsonld"
PLATFORM_CUSTOM = "custom"
PLATFORM_UNKNOWN = "unknown"

CONF_FULL, CONF_PARTIAL, CONF_NONE = "full", "partial", "none"
CORE_FIELDS = ("title", "price", "typology", "location")
SCORED_FIELDS = ("title", "price", "typology", "location", "area")
SAMPLE_SIZE = 3
MAX_SITEMAP_BYTES = 12_000_000
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
LASTMOD_RE = re.compile(r"<lastmod>", re.I)
SITEMAP_IN_ROBOTS_RE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)

# eGO ships the same robots.txt on every customer site; these three rules are the fingerprint. Verified
# byte-identical on residential.jll.pt, dils.pt and quintelaepenalva.pt (2026-10-31).
EGO_ROBOTS_MARKERS = ("/virtual/printproperty.aspx", "/virtual/smartlink.aspx", "gerir-dados-rgpd")
EGO_LISTING_RE = r"/imovel/[^/]+/(\d+)"
EGO_IMAGE_REWRITE = ["Z1280x960", "Z320x240"]   # measured: pHash identical, 10x smaller
PREFERRED_LOCALES = ("pt-pt", "pt_pt", "-pt.", "/pt/", "pt-br")
LISTING_PATH_HINTS = ("imovel", "imoveis", "property", "properties", "propriedade", "anuncio", "venda", "comprar")
NON_LISTING_HINTS = ("blog", "noticia", "news", "equipa", "team", "contact", "sobre", "about", "privacidade",
                     "cookies", "recrutamento", "servico", "agencia", "agentes")


# --- platform detection ------------------------------------------------------------------------

def detect_platform(robots_text: str, sitemap_urls: list[str], feed_urn: str | None = None) -> tuple[str, list[str]]:
    """Platform and the evidence for it. eGO is decided without fetching a single page.

    `feed_urn` is `agencies.source_urn`, which the portal feed already told us: Imovirtual reports the
    advertiser's CRM, and on 2026-10-31 the live values were `urn:partner:ego` (132 agencies),
    `urn:site:local` (65), `urn:partner:maxwork` (55), `urn:partner:century21` (54), `proppy`, `ximo`,
    `zomecrm`. So the platform is often known before the agency's own site is touched at all - the cheapest
    tier of the cascade is one we were already storing and not using.
    """
    evidence: list[str] = []
    if feed_urn and "ego" in feed_urn.lower().split(":")[-1]:
        evidence.append(f"feed:{feed_urn}")
    flat = (robots_text or "").lower()
    hits = [m for m in EGO_ROBOTS_MARKERS if m in flat]
    if len(hits) >= 2:
        evidence.append(f"robots:{hits[0]}")
        if any("sitemap-pt-pt" in u.lower() for u in sitemap_urls):
            evidence.append("sitemap:sitemap-pt-pt.xml")
    if any(e.startswith("robots:") for e in evidence) or any(e.startswith("feed:") for e in evidence):
        return PLATFORM_EGO, evidence
    return PLATFORM_UNKNOWN, evidence


# --- sitemap discovery -------------------------------------------------------------------------

def sitemap_candidates(base_url: str, robots_text: str) -> list[str]:
    declared = [u.strip() for u in SITEMAP_IN_ROBOTS_RE.findall(robots_text or "")]
    root = base_url if base_url.endswith("/") else base_url + "/"
    return list(dict.fromkeys(declared + [urljoin(root, "sitemap.xml"), urljoin(root, "sitemap_index.xml")]))


def fetch_text(client: HttpClient, url: str, delay_s: float) -> str | None:
    try:
        response = client.get(url, delay_s=delay_s, retries=1)
    except Exception as exc:  # noqa: BLE001 - a profiling fetch must never raise into the funnel
        log.info("profiler: %s unreachable (%r)", url, exc)
        return None
    if response.status_code != 200 or not response.content:
        return None
    if len(response.content) > MAX_SITEMAP_BYTES:
        return None
    return response.text


def _is_index(xml: str) -> bool:
    return "<sitemapindex" in xml.lower()


def _prefer_locale(urls: list[str]) -> list[str]:
    """One locale only. Reading every language would multiply the catalogue by three for the same stock."""
    pt = [u for u in urls if any(tag in u.lower() for tag in PREFERRED_LOCALES)]
    return pt or urls


def resolve_index(client: HttpClient, base_url: str, robots_text: str, delay_s: float) -> tuple[list[str], list[str], bool]:
    """-> (index urls to read, listing urls found, lastmod present).

    Follows one level of sitemap index, which is all any of the six real platforms needed.
    """
    for candidate in sitemap_candidates(base_url, robots_text):
        xml = fetch_text(client, candidate, delay_s)
        if not xml:
            continue
        locs = LOC_RE.findall(xml)
        if not locs:
            continue
        if _is_index(xml):
            children = _prefer_locale([u for u in locs if u.lower().endswith((".xml", ".xml.gz"))])
            # Prefer children whose name suggests listings (grupovantagem: sitemap-imoveis.xml).
            ranked = sorted(children, key=lambda u: (0 if any(h in u.lower() for h in LISTING_PATH_HINTS) else 1, len(u)))
            used, listings, lastmod = [], [], False
            for child in ranked[:3]:
                child_xml = fetch_text(client, child, delay_s)
                if not child_xml:
                    continue
                child_locs = LOC_RE.findall(child_xml)
                if not child_locs:
                    continue
                used.append(child)
                listings += child_locs
                lastmod = lastmod or bool(LASTMOD_RE.search(child_xml))
                if len(listings) > 50:
                    break
            if used:
                return used, listings, lastmod
            continue
        return [candidate], locs, bool(LASTMOD_RE.search(xml))
    return [], [], False


# --- listing-URL pattern -----------------------------------------------------------------------

ID_SUFFIX_RE = re.compile(r"^(?P<slug>.*[a-z0-9])-(?P<id>[A-Za-z0-9]{15,})$")


def _shape(path: str) -> str:
    """Path with its variable parts generalised: /imovel/<slug>/21621572 -> /imovel/*/<num>

    `<tail>` is a record id glued to the end of a slug segment, which is how portadafrente carries its
    Salesforce id (`…-oeiras-venda-a0cTg00000IcBzPIAV`). Recognising it gives a short stable external id
    instead of falling back to the whole path.
    """
    parts = []
    for segment in path.strip("/").split("/"):
        if segment.isdigit():
            parts.append("<num>")
        elif re.fullmatch(r"[A-Za-z0-9]{12,}", segment):
            parts.append("<id>")
        elif ID_SUFFIX_RE.match(segment):
            parts.append("<tail>")
        elif "-" in segment:
            parts.append("*")
        else:
            parts.append(segment.lower())
    return "/" + "/".join(parts)


def _generalise(shape: str, shapes: Counter[str]) -> str:
    """Widen the literal segments that the catalogue clearly varies.

    expportugal publishes `/imoveis/<concelho>/apartamento/<slug>` alongside `/imoveis/<concelho>/moradia/…`
    and `/imoveis/<concelho>/terreno/…`: taking the commonest shape verbatim captured apartments only, 228 of
    3 516 real URLs. Both the concelho and the property type vary, so widening a single position is not
    enough.

    The first segment is never widened. It is the anchor that keeps `/blog/…` and `/sobre-nos` out, so the
    result can be broad in the middle but never `/*/*/*`.
    """
    segments = shape.strip("/").split("/")
    family = [s for s in shapes if len(s.strip("/").split("/")) == len(segments) and s != shape]
    if not family:
        return shape
    widen: set[int] = set()
    for other in family:
        other_segments = other.strip("/").split("/")
        if other_segments[0] != segments[0]:
            continue  # a different anchor is a different kind of page, not a variant of this one
        for index, (mine, theirs) in enumerate(zip(segments, other_segments)):
            if index and mine != theirs and mine not in ("<num>", "<id>", "<tail>", "*"):
                widen.add(index)
    for index in widen:
        segments[index] = "*"
    return "/" + "/".join(segments)


def derive_listing_pattern(urls: list[str]) -> tuple[str | None, int, list[str]]:
    """-> (regex, id group, the listing urls that match it).

    Chooses the commonest URL shape that looks like a listing. Agency sitemaps mix pages, blog posts and
    listings, so the shape has to be inferred rather than assumed.
    """
    shapes: Counter[str] = Counter()
    by_shape: dict[str, list[str]] = {}
    for url in urls:
        path = urlparse(url).path
        if not path or path == "/":
            continue
        shape = _shape(path)
        low = shape.lower()
        if any(h in low for h in NON_LISTING_HINTS) and not any(h in low for h in ("imovel", "imoveis")):
            continue
        if not any(token in shape for token in ("<num>", "<id>", "<tail>", "*")):
            continue
        shapes[shape] += 1
        by_shape.setdefault(shape, []).append(url)

    if not shapes:
        return None, 0, []
    ranked = sorted(shapes.items(), key=lambda kv: (0 if any(h in kv[0].lower() for h in LISTING_PATH_HINTS) else 1, -kv[1]))
    shape = _generalise(ranked[0][0], shapes)
    segments = shape.strip("/").split("/")
    parts, group, index = [], 0, 0
    for segment in segments:
        if segment == "<num>":
            index += 1
            group = group or index
            parts.append(r"(\d+)")
        elif segment == "<id>":
            index += 1
            group = group or index
            parts.append(r"([A-Za-z0-9]{12,})")
        elif segment == "<tail>":
            index += 1
            group = group or index
            parts.append(r"[^/]*?-([A-Za-z0-9]{15,})")
        elif segment == "*":
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(segment))
    regex = "/" + "/".join(parts) + r"/?$"
    matched = [u for u in urls if re.search(regex, urlparse(u).path)]
    return regex, group, matched


RESIDENTIAL_SLUG_RE = re.compile(r"apartamento|moradia|penthouse|duplex|\bcasa\b|-t\d")


def _spread(pool: list[str], count: int) -> list[str]:
    if len(pool) <= count:
        return pool
    step = max(1, len(pool) // (count + 1))
    return [pool[min(step * (i + 1), len(pool) - 1)] for i in range(count)]


def pick_samples(urls: list[str], pattern: str | None, count: int = SAMPLE_SIZE, prefer=None) -> list[str]:
    """Which listings to measure the site's confidence on.

    Spread across the catalogue, not the first three: on every real sitemap those are the static pages or
    the oldest stock. And **prefer the kind of listing this system will actually ingest** - residential, in
    her districts. Measured on quintelaepenalva 2026-10-31: an even spread landed on a Douro *quinta* and a
    Setúbal *terreno*, neither of which has a typology, so the site scored `none` on a sample that
    misrepresented the apartments she is actually looking for. `prefer` narrows the pool when it can and is
    ignored when it would leave nothing.
    """
    pool = [u for u in urls if not pattern or re.search(pattern, urlparse(u).path)]
    residential = [u for u in pool if RESIDENTIAL_SLUG_RE.search(urlparse(u).path.lower())]
    if prefer:
        in_scope = [u for u in (residential or pool) if prefer(u)]
        if len(in_scope) >= count:
            return _spread(in_scope, count)
    return _spread(residential or pool, count)


# --- confidence --------------------------------------------------------------------------------

def score(samples: list[set[str]], derived: list[set[str]] | None = None) -> tuple[str, list[str]]:
    """-> (confidence, fields present on at least half the samples).

    `full` needs all five fields **typed by the site**. A site that only yields its typology and area by
    regex over the description - which is every eGO site, since eGO types neither - is `partial` however
    reliably the regex happens to work today: prose can be reworded, a typed field cannot. portadafrente
    and Grupo Vantagem type everything and reach `full`.
    """
    if not samples:
        return CONF_NONE, []
    need = max(1, len(samples) // 2 + len(samples) % 2)
    tally: Counter[str] = Counter()
    for fields in samples:
        tally.update(fields)
    present = sorted(f for f, n in tally.items() if n >= need)
    core = sum(1 for f in CORE_FIELDS if f in present)
    if core < 3:
        return CONF_NONE, present
    if sum(1 for f in SCORED_FIELDS if f in present) < len(SCORED_FIELDS):
        return CONF_PARTIAL, present
    derived_tally: Counter[str] = Counter()
    for fields in derived or []:
        derived_tally.update(fields)
    if any(derived_tally.get(f, 0) >= need for f in SCORED_FIELDS):
        return CONF_PARTIAL, present
    return CONF_FULL, present


def needs_llm(present: list[str]) -> bool:
    """The LLM profiler is reached only when the rules cascade left fewer than 3 core fields."""
    return sum(1 for f in CORE_FIELDS if f in present) < 3


# --- the cascade -------------------------------------------------------------------------------

def build_profile(base_url: str, *, client: HttpClient | None = None, delay_s: float = 2.0,
                  resolve=None, source_key: str = "agency", feed_urn: str | None = None, prefer=None) -> dict:
    """Run the cascade against a live site and return the profile dict for `sources.profile`.

    Never raises: an unreachable or unreadable site is a `none` profile carrying the reason, because a site
    that silently produces nothing is the one outcome the Day 5 gate forbids.
    """
    client = client or get_client()
    profile: dict = {"platform": PLATFORM_UNKNOWN, "detected_by": [], "index": {}, "incremental": "setdiff",
                     "extract": {"engine": "jsonld"}, "confidence": CONF_NONE, "fields_present": [],
                     "checked_at": datetime.now(timezone.utc).isoformat(), "notes": None}

    info = robots_mod.fetch(base_url, client)
    profile["robots_status"] = info.status_code
    profile["content_signal"] = info.content_signal or {}
    if info.status_code not in (200, 404):
        profile["notes"] = f"robots.txt não legível (HTTP {info.status_code}); não foi recolhido nada"
        return profile
    if not info.allows(base_url):
        profile["notes"] = "robots.txt não permite a recolha"
        return profile

    index_urls, catalogue, has_lastmod = resolve_index(client, base_url, info.text, delay_s)
    platform, evidence = detect_platform(info.text, index_urls + sitemap_candidates(base_url, info.text), feed_urn)
    profile["platform"], profile["detected_by"] = platform, evidence
    if not catalogue:
        profile["notes"] = "sem sitemap legível: é preciso um URL de listagem manual"
        return profile

    if platform == PLATFORM_EGO:
        pattern, group = EGO_LISTING_RE, 1
        profile["images"] = {"size_rewrite": EGO_IMAGE_REWRITE}
        matched = [u for u in catalogue if re.search(pattern, urlparse(u).path)]
    else:
        pattern, group, matched = derive_listing_pattern(catalogue)
    if not pattern or not matched:
        profile["notes"] = "não foi possível identificar o padrão dos URLs dos imóveis"
        profile["index"] = {"mode": "sitemap", "urls": index_urls}
        return profile

    profile["index"] = {"mode": "sitemap", "urls": index_urls, "listing_url_re": pattern, "id_group": group,
                        "catalogue_size": len(matched)}
    profile["incremental"] = "lastmod" if has_lastmod else "setdiff"

    samples, derived, probed = [], [], []
    for url in pick_samples(matched, pattern, prefer=prefer):
        page = fetch_text(client, url, delay_s)
        if page is None:
            continue
        canonical = reader.extract(page, url, profile, source_key, resolve=resolve)
        samples.append(reader.resolved_fields(canonical))
        derived.append(reader.derived_fields(canonical))
        probed.append(url)
    profile["samples"] = probed
    profile["derived_fields"] = sorted({f for fields in derived for f in fields})
    profile["confidence"], profile["fields_present"] = score(samples, derived)
    if profile["confidence"] == CONF_NONE and not probed:
        profile["notes"] = "as páginas de detalhe não responderam"
    elif needs_llm(profile["fields_present"]):
        profile["needs_llm"] = True
        profile["notes"] = "estrutura não reconhecida pelas regras; precisa do perfil por IA"
    if platform == PLATFORM_UNKNOWN and profile["confidence"] != CONF_NONE:
        profile["platform"] = PLATFORM_JSONLD
    return profile
