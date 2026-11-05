"""Who she calls, and whether the number can be dialled.

She is about to hand this column to a paid assistant, so a blank costs a lookup and a junk number costs a
wasted call. Measured on production 2026-11-05, before this existed:

  usable number on screen        1 044 / 1 515   68.9 %
  number the system actually had 1 369 / 1 515   90.4 %

The missing fifth sat on the agency record, written by the Imovirtual agency directory, and neither the
panel nor the Excel export ever looked there. Both now go through `properties.contact_for`.

The agency fallback was checked before being trusted, not assumed: of the 271 shown properties where the
listing phone and the agency phone both existed and disagreed, the agency NAME was identical in 271 of
271 - two real lines for the same agency, never a different company.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.listing import normalize_phone, plausible_phone
from app.services.properties import contact_for


def _listing(name=None, phone=None, agency_id=None):
    return SimpleNamespace(agency_name_raw=name, agency_phone_raw=phone, agency_id=agency_id)


def _agency(phone):
    return SimpleNamespace(phone=phone)


# --- what counts as a dialable number -----------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("966007777", "966007777"),
    ("+351 966 007 777", "966007777"),      # formatting and country code are not differences
    ("351966007777", "966007777"),
    ("217520640", "217520640"),             # Lisboa landline
    ("808200030", "808200030"),             # shared-cost "linha azul" - real, and it looks wrong
    ("308806576", "308806576"),             # 30x nomadic / VoIP range - real, and it looks wrong
])
def test_real_numbers_survive(raw, expected):
    """The 808 and 30x ranges are the ones a naive "must start with 2 or 9" rule would throw away. Both
    appear on real agencies in her data."""
    assert plausible_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "990000000",    # the OLX placeholder: 99 is not a Portuguese mobile prefix
    "210000000",    # the "12 12 12" placeholder: valid prefix, six zeros after it
    "99 000 00 00",
    "21752064",     # 8 digits
    "2175206400",   # 10 digits
    "", None,
])
def test_junk_and_malformed_are_refused(raw):
    """Both patterns were observed in production; neither was invented. 62 rows carried them."""
    assert plausible_phone(raw) is None


def test_the_filter_stays_narrow():
    """Only two patterns, because only two were measured. Widening it without new evidence would start
    hiding numbers she could have called."""
    from app.domain.listing import _PHONE_JUNK

    assert len(_PHONE_JUNK) == 2


def test_normalisation_is_separate_from_judgement():
    """`normalize_phone` must not reject anything - a comparison needs the digits of a junk number too."""
    assert normalize_phone("990000000") == "990000000"
    assert plausible_phone("990000000") is None


# --- the fallback chain -------------------------------------------------------------------------

def test_the_listings_own_number_wins():
    """It is the one printed on that advert, so it beats the agency's switchboard."""
    primary = _listing("Dils Portugal", "966007777", 7)
    name, phone = contact_for(primary, [primary], {7: _agency("211111111")})
    assert (name, phone) == ("Dils Portugal", "966007777")


def test_a_sibling_listing_is_used_when_the_primary_is_silent():
    """Only 10 rows on production, but this is the one place a contact is resolved."""
    primary = _listing("RE/MAX Siimgroup", None, 3)
    sibling = _listing("RE/MAX Siimgroup", "217520640", 3)
    assert contact_for(primary, [primary, sibling], {})[1] == "217520640"


def test_the_agency_record_is_the_last_resort():
    """This is the step worth 315 rows, 68.9 % -> 90.4 %."""
    primary = _listing("KW Ábaco", None, 9)
    assert contact_for(primary, [primary], {9: _agency("213514260")})[1] == "213514260"


def test_a_junk_number_on_the_listing_does_not_block_the_fallback():
    """The OLX rows: the advert carries 990000000, the agency record carries a real line."""
    primary = _listing("OLX", "990000000", 9)
    name, phone = contact_for(primary, [primary], {9: _agency("217520640")})
    assert (name, phone) == ("OLX", "217520640")


def test_junk_everywhere_yields_nothing_rather_than_junk():
    """A blank costs her a lookup; a dead number costs a call. Prefer the blank."""
    primary = _listing("12 12 12", "210000000", 4)
    assert contact_for(primary, [primary], {4: _agency("990000000")})[1] is None


def test_the_name_falls_back_to_a_sibling_too():
    primary = _listing(None, None, 3)
    sibling = _listing("Quintela + Penalva", "210201965", 3)
    assert contact_for(primary, [primary, sibling], {}) == ("Quintela + Penalva", "210201965")


def test_no_agency_map_is_not_an_error():
    """`contact_for` is called from the export and the panel; neither may crash if the map is absent."""
    primary = _listing("KW", None, 3)
    assert contact_for(primary, [primary], None) == ("KW", None)
    assert contact_for(primary, [primary], {}) == ("KW", None)


def test_an_unlinked_listing_is_not_an_error():
    primary = _listing("Particular", None, None)
    assert contact_for(primary, [primary], {1: _agency("217520640")}) == ("Particular", None)


# --- the panel and the spreadsheet must not disagree --------------------------------------------

def test_both_the_panel_and_the_export_use_the_one_resolver():
    """Two copies of this logic drifted once already: the export picked the first listing with a phone
    while the panel used only the primary, so the screen and the file named different numbers."""
    import inspect

    from app.services import export
    from app.web.routers import properties as router

    assert "contact_for(" in inspect.getsource(export.rows_for)
    assert "contact_for(" in inspect.getsource(router.list_properties)
    assert "agency_phone_raw" not in inspect.getsource(export.rows_for), "must go through the resolver"
