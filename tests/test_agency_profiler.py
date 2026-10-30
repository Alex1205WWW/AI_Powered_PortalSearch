"""The profiler cascade: fingerprints, listing-URL derivation, and confidence as a measurement.

The URL corpora here are real sitemap slices captured on 2026-10-31, so the derivation is tested against
the shapes that actually exist rather than invented ones.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import re

from app.sources import agency_profiler as profiler

FIXTURES = Path(__file__).parent / "fixtures"


# --- platform fingerprint -----------------------------------------------------------------------

def test_ego_is_identified_from_robots_txt_alone():
    """Its robots.txt is byte-identical across customer sites, so the platform is known before any page is
    fetched - which is what makes one reader cover three of her four named agencies."""
    robots = (FIXTURES / "agency_ego_robots.txt").read_text(encoding="utf-8")
    platform, evidence = profiler.detect_platform(robots, ["https://x.pt/sitemap-pt-pt.xml"])
    assert platform == profiler.PLATFORM_EGO
    assert any("printproperty" in marker for marker in evidence)


def test_a_plain_robots_txt_is_not_mistaken_for_ego():
    platform, evidence = profiler.detect_platform("User-Agent: *\nAllow: *\nCrawl-Delay: 2\n", ["https://x.pt/sitemap.xml"])
    assert platform == profiler.PLATFORM_UNKNOWN
    assert evidence == []


def test_sitemaps_declared_in_robots_are_tried_first():
    found = profiler.sitemap_candidates("https://x.pt", "Sitemap: https://x.pt/sitemap-imoveis.xml\n")
    assert found[0] == "https://x.pt/sitemap-imoveis.xml"
    assert "https://x.pt/sitemap.xml" in found      # the conventional location is still a fallback


# --- listing-URL derivation ---------------------------------------------------------------------

def locs(name: str) -> list[str]:
    return re.findall(r"<loc>([^<]*)</loc>", (FIXTURES / name).read_text(encoding="utf-8"))


def test_ego_sitemap_slice_has_no_lastmod_so_the_mode_is_setdiff():
    """Measured over 15 008 real eGO URLs: zero `<lastmod>` elements. The RE/MAX watermark cannot be reused,
    so withdrawal has to be found by set-difference."""
    xml = (FIXTURES / "agency_ego_sitemap.xml").read_text(encoding="utf-8")
    assert not profiler.LASTMOD_RE.search(xml)
    listings = [u for u in locs("agency_ego_sitemap.xml") if re.search(profiler.EGO_LISTING_RE, urlparse(u).path)]
    assert len(listings) >= 10


def test_a_record_id_glued_to_a_slug_is_recovered_as_the_id():
    """portadafrente carries its Salesforce id at the end of the slug segment. Without recognising it the
    external id would fall back to the whole 85-character path."""
    urls = [f"https://www.portadafrente.com/pt/imoveis/apartamento-t{n}-em-oeiras-venda-a0cTg00000IcBz{n:03d}"
            for n in range(1, 30)]
    pattern, group, matched = profiler.derive_listing_pattern(urls)
    assert group == 1 and len(matched) == len(urls)
    assert re.search(pattern, urlparse(urls[0]).path).group(group) == "a0cTg00000IcBz001"


def test_a_numeric_last_segment_becomes_the_id():
    urls = [f"https://grupovantagem.com/imoveis/moradia-t3-para-venda-rio-maior-{n}/{60000 + n}" for n in range(30)]
    pattern, group, matched = profiler.derive_listing_pattern(urls)
    assert group == 1 and len(matched) == len(urls)
    assert re.search(pattern, urlparse(urls[5]).path).group(group) == "60005"


def test_one_varying_literal_segment_is_widened():
    """expportugal publishes /imoveis/<concelho>/apartamento/<slug> and .../moradia/<slug>. Taking the
    commonest shape verbatim captured apartments only - 228 of 3 516 real URLs."""
    urls = ([f"https://x.pt/imoveis/lisboa/apartamento/lisboa-{n}" for n in range(20)]
            + [f"https://x.pt/imoveis/porto/moradia/porto-{n}" for n in range(15)]
            + [f"https://x.pt/imoveis/faro/terreno/faro-{n}" for n in range(10)])
    pattern, _group, matched = profiler.derive_listing_pattern(urls)
    assert len(matched) == len(urls), f"{pattern} matched only {len(matched)}"


def test_blog_and_company_pages_are_not_taken_for_listings():
    urls = ([f"https://x.pt/blog/artigo-{n}" for n in range(40)]
            + [f"https://x.pt/imoveis/casa-t3-em-oeiras/{n}" for n in range(12)])
    pattern, _group, matched = profiler.derive_listing_pattern(urls)
    assert "imoveis" in pattern
    assert all("/blog/" not in u for u in matched)


def test_no_recognisable_shape_is_an_honest_none():
    assert profiler.derive_listing_pattern(["https://x.pt/", "https://x.pt/sobre"]) == (None, 0, [])


def test_samples_are_spread_across_the_catalogue():
    """The first three URLs of a real sitemap are the static pages or the oldest stock."""
    urls = [f"https://x.pt/imovel/casa/{n}" for n in range(300)]
    picked = profiler.pick_samples(urls, r"/imovel/[^/]+/(\d+)")
    assert len(picked) == profiler.SAMPLE_SIZE
    assert len(set(picked)) == profiler.SAMPLE_SIZE
    assert picked != urls[:3]


# --- confidence is a measurement ----------------------------------------------------------------

def test_everything_typed_is_full():
    assert profiler.score([{"title", "price", "typology", "location", "area"}] * 3)[0] == profiler.CONF_FULL


def test_an_ego_shaped_result_is_partial_not_full():
    """eGO gives price, typology and area but no resolvable address node; `partial` is the ceiling of what
    the site publishes, not a shortcoming of the reader."""
    confidence, present = profiler.score([{"title", "price", "typology", "area"}] * 3)
    assert confidence == profiler.CONF_PARTIAL
    assert "location" not in present


def test_fewer_than_three_core_fields_is_none():
    assert profiler.score([{"title", "price"}] * 3)[0] == profiler.CONF_NONE
    assert profiler.score([set(), set(), set()])[0] == profiler.CONF_NONE
    assert profiler.score([])[0] == profiler.CONF_NONE


def test_a_field_present_on_only_one_sample_is_not_claimed():
    _confidence, present = profiler.score([{"title", "price", "typology", "location"},
                                           {"title", "price", "typology"},
                                           {"title", "price", "typology"}])
    assert "location" not in present


def test_the_llm_is_reached_only_when_the_rules_fall_short():
    assert profiler.needs_llm(["title", "price"]) is True
    assert profiler.needs_llm(["title", "price", "typology"]) is False
    assert profiler.needs_llm(["title", "price", "typology", "location", "area"]) is False


# --- confidence must not overstate a prose-derived field ----------------------------------------

def test_a_prose_derived_core_field_caps_confidence_at_partial():
    """eGO types neither area nor typology: the reader recovers them from the title and description, which
    works today but can be reworded. Reporting that as `full` would overstate it to her. Measured live
    2026-10-31: portadafrente `full` (everything typed), dils.pt and quintelaepenalva `partial`."""
    everything = {"title", "price", "typology", "location", "area"}
    assert profiler.score([everything] * 3, [set()] * 3)[0] == profiler.CONF_FULL
    assert profiler.score([everything] * 3, [{"area"}] * 3)[0] == profiler.CONF_PARTIAL
    assert profiler.score([everything] * 3, [{"area", "typology", "location"}] * 3)[0] == profiler.CONF_PARTIAL


def test_one_odd_prose_sample_does_not_demote_a_typed_site():
    everything = {"title", "price", "typology", "location", "area"}
    assert profiler.score([everything] * 3, [{"area"}, set(), set()])[0] == profiler.CONF_FULL


# --- sampling must measure the stock the system will actually ingest ----------------------------

def test_samples_prefer_residential_listings():
    """An even spread over quintelaepenalva's real catalogue landed on a Douro quinta and a Setúbal terreno,
    neither of which has a typology, and scored the site `none` - on stock she never searches for."""
    urls = ([f"https://x.pt/imovel/terreno-industrial-em-setubal/{n}" for n in range(60)]
            + [f"https://x.pt/imovel/apartamento-t2-em-lisboa/{n}" for n in range(200, 230)])
    picked = profiler.pick_samples(urls, r"/imovel/[^/]+/(\d+)")
    assert all("apartamento" in u for u in picked)


def test_samples_prefer_her_districts_when_enough_of_them_exist():
    urls = ([f"https://x.pt/imovel/apartamento-t2-no-porto/{n}" for n in range(60)]
            + [f"https://x.pt/imovel/apartamento-t2-em-oeiras/{n}" for n in range(200, 230)])
    picked = profiler.pick_samples(urls, r"/imovel/[^/]+/(\d+)", prefer=lambda u: "oeiras" in u)
    assert all("oeiras" in u for u in picked)


def test_the_district_preference_is_dropped_rather_than_returning_nothing():
    """A national agency with no stock in her districts still has to be profiled, not skipped."""
    urls = [f"https://x.pt/imovel/apartamento-t2-no-porto/{n}" for n in range(40)]
    picked = profiler.pick_samples(urls, r"/imovel/[^/]+/(\d+)", prefer=lambda u: "oeiras" in u)
    assert len(picked) == profiler.SAMPLE_SIZE


# --- the platform hint the portal feed already gave us -----------------------------------------

def test_the_crm_reported_by_the_portal_identifies_ego_with_no_fetch():
    """`agencies.source_urn` was already being stored and never used. Live values 2026-10-31:
    `urn:partner:ego` on 132 agencies, `urn:site:local` 65, `urn:partner:maxwork` 55, `century21` 54."""
    platform, evidence = profiler.detect_platform("User-agent: *\nDisallow:\n", [], feed_urn="urn:partner:ego")
    assert platform == profiler.PLATFORM_EGO
    assert evidence == ["feed:urn:partner:ego"]


def test_another_crm_is_not_assumed_to_be_ego():
    platform, _evidence = profiler.detect_platform("User-agent: *\n", [], feed_urn="urn:partner:maxwork")
    assert platform == profiler.PLATFORM_UNKNOWN
