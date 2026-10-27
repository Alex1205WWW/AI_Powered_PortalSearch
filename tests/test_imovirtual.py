import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.listing import CanonicalListing
from app.sources import imovirtual as imo

FIX = Path(__file__).parent / "fixtures"


def load_search():
    data = json.loads((FIX / "imovirtual_search.json").read_text(encoding="utf-8"))
    return {"props": {"pageProps": {"data": {"searchAds": data["searchAds"]}}}}


def load_detail():
    data = json.loads((FIX / "imovirtual_detail.json").read_text(encoding="utf-8"))
    return {"props": {"pageProps": {"ad": data["ad"]}}}


def test_extract_next_data_tolerates_extra_attributes():
    html = '<html><script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">{"a": 1}</script></html>'
    assert imo.extract_next_data(html) == {"a": 1}
    assert imo.extract_next_data("<html>no json</html>") is None


def test_search_url_newest_first_with_price_filters():
    url = imo.search_url("moradia", "lisboa", "oeiras", page=2, price_min=400000, price_max=650000)
    assert url == "https://www.imovirtual.com/pt/resultados/comprar/moradia/lisboa/oeiras?by=LATEST&direction=DESC&page=2&priceMin=400000&priceMax=650000"


def test_parse_and_normalise_search_fixture():
    items, pagination = imo.parse_search(load_search())
    assert len(items) == 14 and pagination["itemsPerPage"] == 36
    first = imo.normalise_item(items[0], default_estate="moradia")
    assert first.external_id == "1iZ6K" and first.raw["id"] == 19301142  # keyed by the slug token, not the numeric id
    assert first.url == "https://www.imovirtual.com/pt/anuncio/moradia-t2-para-venda-ID1iZ6K"
    assert first.price == 430000 and first.currency == "EUR" and not first.hide_price
    assert first.rooms == 3 and first.estate_type == "moradia" and first.transaction == "sale"
    assert first.area_util == 186.56 and first.land_area is None
    assert first.geo_code == "lisboa/oeiras/barcarena"
    assert [p[1] for p in first.geo_path] == ["district", "council", "parish"]
    assert first.agency_name == "RE/MAX + Grupo Vantagem" and first.agency_slug == "re-max-grupo-vantagem-ID4009208"
    assert first.agency_urn == "urn:partner:maxwork"
    assert first.published_at == datetime(2026, 10, 8, 3, 8, 43, tzinfo=timezone.utc)
    assert first.images and first.images[0].startswith("https://")
    assert first.street == "Travessa 31 de Janeiro"
    rooms = sorted(imo.normalise_item(i, "moradia").rooms for i in items)
    assert rooms[0] >= 2 and 10 in rooms  # TEN maps to 10; open upper bound works


def test_every_fixture_item_normalises_with_key_fields():
    items, _ = imo.parse_search(load_search())
    undated = 0
    for item in items:
        listing = imo.normalise_item(item, "moradia")
        assert listing.external_id and listing.url and listing.geo_code
        assert listing.price is not None and listing.rooms is not None
        undated += listing.published_at is None
    # One real item carries dateCreated "1999-02-29 00:00:01" (an impossible date): tolerated as unknown, never a crash.
    assert undated == 1


def test_promoted_tiles_are_keyed_by_their_real_listing():
    items, _ = imo.parse_search(load_search())
    tiles = [i for i in items if int(i["id"]) >= 9_000_000_000]
    assert tiles, "fixture should contain a synthetic promoted tile"
    for tile in tiles:
        listing = imo.normalise_item(tile, "moradia")
        assert listing.external_id == imo.public_id(tile["slug"])
        assert listing.external_id != str(tile["id"])
    assert imo.public_id("moradia-t2-para-venda-ID1iZ6K") == "1iZ6K"
    assert imo.public_id("sem-id-no-slug") is None and imo.public_id(None) is None


def test_parse_detail_fixture():
    detail = imo.parse_detail(load_detail())
    assert detail["status"] == "active" and detail["price"] == 430000 and not detail["hide_price"]
    assert detail["rooms"] == 3 and detail["wc"] == 2 and detail["build_year"] == 1951
    assert detail["reference"] == "125011292-32"
    assert detail["agency_phone"] == "+351808200030"  # office number, not the agent's personal phone
    assert abs(detail["lat"] - 38.732204) < 1e-6 and detail["geo_code"] == "lisboa/oeiras/barcarena"
    assert "Barcarena" in detail["description"] and "<" not in detail["description"]
    items, _ = imo.parse_search(load_search())
    listing = imo.apply_detail(imo.normalise_item(items[0], "moradia"), detail)
    assert listing.wc == 2 and listing.reference == "125011292-32" and listing.agency_phone == "+351808200030"
    assert "&nbsp;" not in listing.description
    assert listing.features == {}  # this description names no amenity: honest "não indicado", nothing invented


def test_typed_features_become_confirmed_tier():
    ad = {"status": "active", "target": {"Price": 500000, "Rooms_num": ["3"], "hidePrice": "0"},
          "featuresByCategory": [{"label": "extras", "values": ["garage", "lift"]}], "features": ["garden"],
          "description": "Apartamento com varanda &amp; vista de rio", "location": {}, "agency": {"phones": ["+351210000000"]}}
    detail = imo.parse_detail({"props": {"pageProps": {"ad": ad}}})
    listing = imo.apply_detail(CanonicalListing(source_key="imovirtual", external_id="x", url="u", title="T3"), detail)
    assert listing.features["garagem"]["tier"] == "confirmed"
    assert listing.features["elevador"]["tier"] == "confirmed"
    assert listing.features["exterior"]["tier"] == "confirmed"  # garden implies outdoor space
    assert listing.features["varanda"]["tier"] == "mentioned" and listing.features["vista"]["tier"] == "mentioned"


def test_paginate_newest_stops_at_watermark():
    now = datetime.now(timezone.utc)

    def mk(i, age_minutes):
        return CanonicalListing(source_key="imovirtual", external_id=str(i), url=f"u{i}", published_at=now - timedelta(minutes=age_minutes))

    pages = {
        1: ([mk(1, 5), mk(2, 30), mk(3, 60)], {"totalPages": 3}),
        2: ([mk(4, 120), mk(5, 300)], {"totalPages": 3}),
        3: ([mk(6, 900)], {"totalPages": 3}),
    }
    calls = []

    def fetch(page):
        calls.append(page)
        return pages[page]

    got = [l.external_id for l in imo.paginate_newest(fetch, since=now - timedelta(minutes=45))]
    assert got == ["1", "2"] and calls == [1]  # grace of 10 min: item 3 (60 min) is below the cutoff -> stop after page 1

    got_all = [l.external_id for l in imo.paginate_newest(fetch, since=None)]
    assert got_all == ["1", "2", "3", "4", "5", "6"]
