"""The generic agency reader, against real pages captured on 2026-10-31.

Every fixture is a real extract of a live page (title, OpenGraph metas and the JSON-LD blocks, which are the
only parts the reader looks at). The four cover the platform diversity that was actually measured:
eGO with a price, eGO without one, and two own-platform sites - one of which wraps its JSON-LD in `@graph`
and keeps the price on a sibling `Offer` node.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.sources import agency_generic as reader

FIXTURES = Path(__file__).parent / "fixtures"

EGO_PROFILE = {"index": {"listing_url_re": r"/imovel/[^/]+/(\d+)", "id_group": 1},
               "images": {"size_rewrite": ["Z1280x960", "Z320x240"]}}
PF_PROFILE = {"index": {"listing_url_re": r"/pt/imoveis/[^/]*?-([A-Za-z0-9]{15,})/?$", "id_group": 1}}
GV_PROFILE = {"index": {"listing_url_re": r"/imoveis/[^/]+/(\d+)/?$", "id_group": 1}}

EGO_URL = "https://www.quintelaepenalva.pt/imovel/apartamento-t3-para-venda-em-alcantara-lisboa/21621572"
EGO_NOPRICE_URL = "https://residential.jll.pt/imovel/apartamento-t2-duplex-com-estacionamento-odeon-lisboa/8023566"
PF_URL = ("https://www.portadafrente.com/pt/imoveis/"
          "apartamento-t1-no-future-mirear-miraflores-oeiras-venda-a0cTg00000IcBzPIAV")
GV_URL = "https://grupovantagem.com/imoveis/moradia-para-venda-496000-eur-123551375-49/67814"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def extract(name: str, url: str, profile: dict, resolve=None):
    return reader.extract(read(name), url, profile, "agency:test", resolve=resolve)


# --- eGO: the platform three of her four sites run on ------------------------------------------

def test_ego_listing_yields_price_typology_area_and_contact():
    listing = extract("agency_ego_listing.html", EGO_URL, EGO_PROFILE)
    assert listing.external_id == "21621572"
    assert listing.price == 2_500_000          # typed in offers
    assert listing.rooms == 3                  # from the title: eGO never types typology
    assert listing.area_util == 177.0          # from the description: eGO never types area either
    assert listing.estate_type == "apartamento"
    assert listing.transaction == "sale"
    assert listing.agency_name == "Quintela + Penalva | Knight Frank"
    assert listing.agency_phone == "212454059"


def test_ego_html_entities_are_unescaped_before_the_zone_is_read():
    """eGO emits `Alc&#226;ntara` inside valid UTF-8 JSON-LD. Left escaped, no zone would ever resolve."""
    listing = extract("agency_ego_listing.html", EGO_URL, EGO_PROFILE)
    assert "Alcântara" in listing.title
    assert "&#" not in listing.title
    assert "Alcântara" in listing.raw["zone_text"]


def test_ego_without_a_price_is_hidden_not_zero():
    """JLL and Dils leave `offers.price` out on their luxury stock ("sob consulta")."""
    listing = extract("agency_ego_nopric.html", EGO_NOPRICE_URL, EGO_PROFILE)
    assert listing.price is None
    assert listing.hide_price is True
    assert "preco" in listing.missing_fields()
    assert listing.rooms == 2                  # still readable from the title


def test_ego_images_are_rewritten_to_the_small_variant():
    """pHash is size-stable (320x240 vs 1280x960 -> Hamming 0), so the 23 KB variant is hashed instead of
    the 241 KB one."""
    listing = extract("agency_ego_listing.html", EGO_URL, EGO_PROFILE)
    assert listing.images and all("Z320x240" in url for url in listing.images)
    assert not any("Z1280x960" in url for url in listing.images)


# --- own-platform sites: richer than the portals ------------------------------------------------

def test_portadafrente_types_everything_including_amenities():
    listing = extract("agency_pf_listing.html", PF_URL, PF_PROFILE)
    assert listing.external_id == "a0cTg00000IcBzPIAV"
    assert listing.price == 504_000
    assert listing.rooms == 1                  # numberOfBedrooms, not guessed from the title
    assert listing.area_util == 65.0           # floorSize
    assert listing.published_at is not None    # datePosted
    # The amenity list is what gives Raquel a real "elevador confirmado" instead of "não indicado".
    assert listing.features["elevador"]["tier"] == "confirmed"
    assert listing.features["garagem"]["tier"] == "confirmed"
    assert len(listing.images) >= 4


def test_grupovantagem_graph_is_flattened_and_the_sibling_offer_is_found():
    """Its JSON-LD sits in `@graph` and `RealEstateListing.offers` is only an `@id` reference: the price
    lives on a separate `Offer` node, so the graph has to be flattened before anything is read."""
    listing = extract("agency_gv_listing.html", GV_URL, GV_PROFILE)
    assert listing.external_id == "67814"
    assert listing.price == 496_000
    assert listing.rooms == 4
    assert listing.area_util == 181.0
    assert listing.estate_type == "moradia"
    assert listing.agency_phone


# --- the typology trap --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Apartamento T1+1 em novo empreendimento no Campo Grande, Lisboa", 1),
    ("Apartamento T2 Duplex com estacionamento, ODEON, Lisboa", 2),
    ("Moradia T4 para venda", 4),
    ("Apartamento T0 no centro", 0),
    ("Apartamento T21 em Alfama", None),   # a bare two-digit T is a compound written without the sign
])
def test_typology_from_text_reads_compounds_as_their_base(text, expected):
    assert reader.typology_from_text(text) == expected


@pytest.mark.parametrize("slug,expected", [
    ("/imovel/apartamento-t3-para-venda-em-alcantara-lisboa/1", 3),
    ("/imovel/apartamento-t11-em-novo-empreendimento-no-campo-grande/1", None),
    ("/imovel/apartamento-t21-em-novo-empreendimento-em-alfama-lisboa/1", None),
    ("/imovel/moradia-sem-tipologia/1", None),
])
def test_typology_from_slug_refuses_two_digit_compounds(slug, expected):
    """`t11` in an eGO slug is "T1+1", not eleven bedrooms. Measured over ~13 500 slugs: 950 compounds
    (t11, t12, t21, t31, t41, t51 ... t82) against a single genuine t10. Reading one as T11 would drop a
    real T1+1 out of Raquel's T1-T2 filter, so a two-digit slug value is refused rather than guessed."""
    assert reader.typology_from_slug("https://x" + slug) == expected


# --- ids, developments, honesty -----------------------------------------------------------------

def test_external_id_never_overflows_the_column_without_a_pattern():
    listing = extract("agency_pf_listing.html", PF_URL, {})
    assert 0 < len(listing.external_id) <= reader.ID_MAX


def test_long_pathless_fallback_keeps_a_hash_so_two_listings_cannot_collide():
    long_a = "https://x.pt/" + "a" * 200 + "/one"
    long_b = "https://x.pt/" + "a" * 200 + "/two"
    first, second = reader.external_id(long_a, {}), reader.external_id(long_b, {})
    assert first != second
    assert len(first) <= reader.ID_MAX and len(second) <= reader.ID_MAX


def test_new_developments_are_flagged_from_the_description_too():
    """`dedup.photo_match` refuses to merge developments because every unit shares the same renders, and eGO
    gives only one hashable image, so missing the flag is how two different flats would become one."""
    listing = extract("agency_ego_listing.html", EGO_URL, EGO_PROFILE)
    assert listing.is_new_development is True


def test_resolved_fields_does_not_claim_a_location_it_never_resolved():
    """Confidence is measured from this. Counting "there was a title" as a location would inflate every
    site to `full`."""
    without = extract("agency_ego_listing.html", EGO_URL, EGO_PROFILE)
    assert "location" not in reader.resolved_fields(without)
    with_zone = extract("agency_ego_listing.html", EGO_URL, EGO_PROFILE,
                        resolve=lambda text: ("lisboa/lisboa/alcantara", "parish"))
    assert with_zone.geo_code == "lisboa/lisboa/alcantara"
    assert "location" in reader.resolved_fields(with_zone)


def test_a_page_with_no_listing_returns_none_rather_than_an_empty_row():
    assert reader.extract("<html><head></head><body>nada</body></html>", "https://x.pt/a", {}, "agency:test") is None


def test_amenity_absence_is_never_read_as_absent():
    """Rosa hides unknown must-haves, so inferring "not in the list" = "does not have it" would silently
    drop real matches. Whether these lists are exhaustive has not been measured."""
    typed = reader.features_from_amenities(["Elevador", "Arrecadação"])
    assert typed == {"elevador": True, "arrecadacao": True}
    assert "piscina" not in typed      # not listed -> unknown, not False
