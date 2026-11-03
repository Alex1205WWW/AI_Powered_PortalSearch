"""RE/MAX adapter, against fixtures captured from live pages on 2026-10-30.

`remax_sitemap.xml` is a 12-entry slice of the real sitemap, chosen to carry every edge case the filter has
to survive. `remax_listings.json` holds six real `listingEncoded` payloads; `remax_detail.html` is one whole
page, so the extractor is exercised on real markup and not only on pre-decoded JSON.
"""
from __future__ import annotations

import csv
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.listing import TIER_ABSENT, TIER_CONFIRMED, TIER_UNKNOWN, feature_tier
from app.sources import remax

FIXTURES = Path(__file__).parent / "fixtures"
GEO_CSV = Path(__file__).resolve().parents[1] / "data" / "geo_pt.csv"


@pytest.fixture(scope="module")
def councils() -> dict[str, str]:
    """{council slug: geo code}, longest first - the same ordering `council_index` builds from the database."""
    pairs = []
    with GEO_CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["level"] == "council":
                pairs.append((row["code"].split("/")[-1], row["code"]))
    return dict(sorted(pairs, key=lambda p: len(p[0]), reverse=True))


@pytest.fixture(scope="module")
def payloads() -> dict:
    return json.loads((FIXTURES / "remax_listings.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sitemap_xml() -> str:
    return (FIXTURES / "remax_sitemap.xml").read_text(encoding="utf-8")


def canonical(payloads, name, councils):
    payload = remax.strip_pii(payloads[name])
    slug = payload["descriptionTags"]
    url = f"https://remax.pt/pt/imoveis/{slug}/{payload['listingTitle']}"  # listingTitle is the URL id
    info = remax.parse_slug(slug, councils)
    assert info is not None, f"{slug} did not resolve to a council"
    return remax.normalise(payload, url, info)


# --- sitemap reading ----------------------------------------------------------------------------

def test_gunzip_sniffs_the_magic_instead_of_assuming():
    """The .xml.gz files arrive already decompressed when the client negotiates Content-Encoding."""
    plain = b"<urlset></urlset>"
    assert remax.maybe_gunzip(plain) == plain
    assert remax.maybe_gunzip(gzip.compress(plain)) == plain


def test_listing_sitemaps_picks_only_the_listing_files():
    index = """<sitemapindex>
      <sitemap><loc>https://remax.pt/sitemap/institutional_pt_1.xml.gz</loc></sitemap>
      <sitemap><loc>https://remax.pt/sitemap/agents_details_pt_1.xml.gz</loc></sitemap>
      <sitemap><loc>https://remax.pt/sitemap/listings_details_pt_1.xml.gz</loc></sitemap>
      <sitemap><loc>https://remax.pt/sitemap/listings_details_pt_4.xml.gz</loc></sitemap>
    </sitemapindex>"""
    assert remax.listing_sitemaps(index) == [
        "https://remax.pt/sitemap/listings_details_pt_1.xml.gz",
        "https://remax.pt/sitemap/listings_details_pt_4.xml.gz",
    ]


def test_entries_tolerate_a_missing_lastmod(sitemap_xml):
    entries = list(remax.iter_entries(sitemap_xml))
    assert len(entries) == 12
    assert sum(1 for _, mod in entries if mod is None) == 1


# --- the slug pre-filter ------------------------------------------------------------------------

def test_slug_filter_keeps_residential_sales_in_her_districts(sitemap_xml, councils):
    refs = remax.select_refs(remax.iter_entries(sitemap_xml), councils, since=None)
    kept = {url for url, _, _ in refs}
    assert len(kept) == 6, kept
    assert all("/pt/imoveis/venda-" in url for url in kept)
    # dropped: arrendamento, terreno, loja, another district (Porto), Loulé, and the bare /pt/<id> twin
    assert not any("arrendamento" in url or "venda-terreno" in url or "venda-loja" in url for url in kept)
    assert not any("-porto-" in url or "-loule-" in url for url in kept)
    assert not any(url.rstrip("/").count("/") == 4 for url in kept)


def test_multiword_council_wins_over_a_shorter_prefix(councils):
    info = remax.parse_slug("venda-apartamento-t2-vila-franca-de-xira-vialonga", councils)
    assert info.council_code == "lisboa/vila-franca-de-xira"
    assert info.parish_slug == "vialonga"
    assert (info.rooms, info.estate_type, info.transaction) == (2, "apartamento", "sale")


def test_council_named_like_its_district_resolves_to_the_council(councils):
    info = remax.parse_slug("venda-moradia-t4-santarem-sao-vicente-do-paul-e-vale-de-figueira", councils)
    assert info.council_code == "santarem/santarem"


def test_empty_typology_does_not_raise(councils):
    """17 130 of 50 000 slugs are non-residential with no typology: `venda-terreno-t<concelho>`."""
    assert remax.parse_slug("venda-terreno-tsintra-colares", councils) is None  # rejected on type, not by a crash
    info = remax.parse_slug("venda-apartamento-t-lisboa-alvalade", councils)
    assert info is not None and info.rooms is None


def test_since_excludes_unchanged_entries_but_keeps_undated_ones(sitemap_xml, councils):
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    refs = remax.select_refs(remax.iter_entries(sitemap_xml), councils, since=future)
    assert len(refs) == 1 and refs[0][2] is None


# --- the detail payload -------------------------------------------------------------------------

def test_extractor_finds_listing_encoded_in_a_real_page():
    payload = remax.extract_listing_encoded((FIXTURES / "remax_detail.html").read_text(encoding="utf-8"))
    assert payload is not None and len(payload) == 153
    assert payload["descriptionTags"] == "venda-apartamento-t2-mafra-milharado"


def test_typology_comes_from_bedrooms_not_typology_id(payloads):
    """typologyID = 2*bedrooms + 1 on all nine sampled listings. Using it would repeat the Imovirtual defect."""
    for name, payload in payloads.items():
        assert payload["typologyID"] == 2 * payload["numberOfBedrooms"] + 1, name


def test_rooms_match_the_slug(payloads, councils):
    for name in payloads:
        listing = canonical(payloads, name, councils)
        slug_t = int(listing.raw["descriptionTags"].split("-t")[1].split("-")[0])
        assert listing.rooms == slug_t, name


def test_a_price_rise_is_history_and_never_a_reduction(payloads, councils):
    listing = canonical(payloads, "mafra_t2_price_rise", councils)
    assert listing.price == 410000 and listing.previous_price == 393000
    assert listing.price_reduced_at is None  # previousPrice alone must never read as a drop


def test_a_real_reduction_is_dated(payloads, councils):
    listing = canonical(payloads, "campolide_t4_price_drop", councils)
    assert listing.previous_price > listing.price
    assert listing.price_reduced_at is not None


def test_sold_wins_over_is_active(payloads, councils):
    """isSold is True while isActive is also True, and the listing stays in the sitemap."""
    payload = payloads["almada_t3_sold_price_drop"]
    assert payload["isSold"] is True and payload["isActive"] is True
    assert canonical(payloads, "almada_t3_sold_price_drop", councils).status == "sold"


def test_pii_never_reaches_the_canonical_listing(payloads, councils):
    forbidden = {"userEmail", "userCellPhone", "userName", "userPublicName", "userID", "agentID", "userPictureURL"}
    for name in payloads:
        assert forbidden & set(payloads[name]), f"{name}: the raw page should contain PII to begin with"
        listing = canonical(payloads, name, councils)
        assert not forbidden & set(listing.raw), name
        blob = json.dumps(listing.raw)
        for field in forbidden:
            assert field not in blob, f"{name} leaked {field}"


def test_agency_phone_falls_back_to_the_agency_switchboard(payloads, councils):
    """officePhoneNumber is null on 8/8 live listings; without the fallback, deliverable 12 ships empty."""
    for name in payloads:
        listing = canonical(payloads, name, councils)
        assert listing.agency_name, name
        assert listing.agency_phone, name


def test_outdoor_space_is_a_typed_tri_state(payloads, councils):
    with_land = canonical(payloads, "lourinha_t3_lot", councils)
    assert with_land.land_area == 471
    assert feature_tier(with_land.features, "exterior") == TIER_CONFIRMED

    stated_none = canonical(payloads, "mafra_t2_price_rise", councils)  # exteriorPrivateArea == 0
    assert feature_tier(stated_none.features, "exterior") == TIER_ABSENT


def test_lift_is_a_typed_tri_state(payloads, councils):
    assert feature_tier(canonical(payloads, "campolide_t4_price_drop", councils).features, "elevador") == TIER_CONFIRMED
    assert feature_tier(canonical(payloads, "grandola_t1_no_lift", councils).features, "elevador") == TIER_ABSENT
    assert feature_tier(canonical(payloads, "palmela_t5_moradia", councils).features, "elevador") == TIER_UNKNOWN


def test_areas_use_living_and_built_never_total(payloads, councils):
    listing = canonical(payloads, "lourinha_t3_lot", councils)
    assert (listing.area_util, listing.area_bruta) == (393.0, 653.0)


def test_images_are_absolute_cdn_urls(payloads, councils):
    listing = canonical(payloads, "mafra_t2_price_rise", councils)
    assert listing.images and all(u.startswith("https://i.maxwork.pt/ds-l/listings/") for u in listing.images)
    assert len(listing.images) <= 10


def test_geo_path_is_district_council_parish(payloads, councils):
    listing = canonical(payloads, "mafra_t2_price_rise", councils)
    assert listing.geo_code == "lisboa/mafra/milharado"
    assert [code for code, _, _ in listing.geo_path] == ["lisboa", "lisboa/mafra", "lisboa/mafra/milharado"]


def test_title_is_built_from_the_slug_not_the_useless_fields(payloads, councils):
    listing = canonical(payloads, "mafra_t2_price_rise", councils)
    assert listing.raw["listingTitle"] == "120612480-104"  # the source's own title is just the id
    assert listing.title == "Apartamento T2 — Mafra, Milharado"


def test_same_named_council_in_another_district_is_rejected(councils):
    info = remax.parse_slug("venda-apartamento-t2-lisboa-santo-antonio", councils)
    assert remax.council_matches_payload(info, {"regionName1": "Lisboa"})
    assert not remax.council_matches_payload(info, {"regionName1": "Porto"})
    assert remax.council_matches_payload(info, {})  # nothing stated: do not reject


# --- resumability -------------------------------------------------------------------------------

def _refs(n):
    return [(f"https://remax.pt/pt/imoveis/venda-apartamento-t1-lisboa-alvalade/1-{i}", None, None) for i in range(n)]


def test_resume_prefers_the_recorded_url_over_the_recorded_index():
    refs = _refs(10)
    assert remax._resume_at(refs, None) == 0
    assert remax._resume_at(refs, {"index": 4, "last_url": refs[3][0]}) == 4
    # the catalogue shifted by two entries: the URL still finds the right place
    assert remax._resume_at(refs[2:], {"index": 4, "last_url": refs[3][0]}) == 2
    # the URL is gone entirely: fall back to the index, clamped
    assert remax._resume_at(refs, {"index": 99, "last_url": "https://remax.pt/pt/imoveis/x/9-9"}) == 10


def _priced(value):
    """A minimal real payload with only the price varied, so nothing else is under test."""
    return {"listingID": "9", "listingTitle": "9", "listingPrice": value,
            "businessTypeID": 1, "numberOfBedrooms": 3, "isActive": True,
            "descriptionTags": "venda-moradia-t3-loures-santo-antao"}


def test_a_sentinel_price_is_no_price_at_all(councils):
    """RE/MAX sends `listingPrice: -1` for "sob consulta". Stored literally it renders as "-1 EUR" and
    satisfies every "price at most X" filter - 31 live listings were affected (found 2026-10-31)."""
    payload = _priced(-1)
    info = remax.parse_slug(payload["descriptionTags"], councils)
    listing = remax.normalise(payload, "https://remax.pt/pt/imoveis/venda-moradia-t3-loures-santo-antao/123456789-1", info)
    assert listing.price is None
    assert listing.hide_price is True
    assert "preco" in listing.missing_fields()


def test_a_zero_price_is_also_no_price(councils):
    payload = _priced(0)
    info = remax.parse_slug(payload["descriptionTags"], councils)
    assert remax.normalise(payload, "https://remax.pt/pt/imoveis/venda-moradia-t3-loures-santo-antao/123456789-1", info).price is None


def test_a_real_price_is_untouched(councils):
    payload = _priced(450000)
    info = remax.parse_slug(payload["descriptionTags"], councils)
    listing = remax.normalise(payload, "https://remax.pt/pt/imoveis/venda-moradia-t3-loures-santo-antao/123456789-1", info)
    assert listing.price == 450000 and listing.hide_price is False


# --- a feed scale error is not a price change (2026-11-02) ---------------------------------------

def test_a_thousandfold_previous_price_is_rejected_as_a_scale_error():
    """RE/MAX `previousPrice` carries occasional scale errors. Measured on all 3 277 source-reported
    previous prices: every plausible one is 0.36x-2.3x of the current price, and the seven outside that
    band are exact powers of ten with the same digits. The Excel export prints this row as "preco
    anterior", so an unfiltered one reaches her as "this house used to cost 2 000 000 000 EUR"."""
    from app.domain.listing import PRICE_SCALE_LIMIT, plausible_previous_price

    assert PRICE_SCALE_LIMIT == 10
    # the real scale errors found in production
    assert plausible_previous_price(2_000_000_000, 2_000_000) is None     # x1000
    assert plausible_previous_price(830_000_000, 830_000) is None         # x1000
    assert plausible_previous_price(439, 439_000) is None                 # x1/1000
    assert plausible_previous_price(39_900, 399_000) is None              # x1/10
    assert plausible_previous_price(8_850, 88_500) is None                # x1/10
    # the most extreme REAL changes observed must survive
    assert plausible_previous_price(580_000, 250_000) == 580_000          # 2.3x drop
    assert plausible_previous_price(530_000, 1_470_000) == 530_000        # 0.36x rise
    assert plausible_previous_price(220_000, 150_000) == 220_000
    # degenerate inputs
    assert plausible_previous_price(None, 300_000) is None
    assert plausible_previous_price(300_000, None) is None
    assert plausible_previous_price(0, 300_000) is None
