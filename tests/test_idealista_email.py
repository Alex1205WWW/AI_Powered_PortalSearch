"""Idealista alert parsing on the saved fixture (synthetic layout, re-validated against the first real email)."""
from datetime import datetime, timezone
from pathlib import Path

from imap_tools import MailMessage

from app.sources import idealista_email as mail

FIX = Path(__file__).parent / "fixtures" / "idealista_alert.eml"


def load_message() -> MailMessage:
    return MailMessage.from_bytes(FIX.read_bytes())


def fake_resolve(tokens):
    if tokens and tokens[-1] == "Lisboa":
        return "lisboa/lisboa", [("lisboa", "district", "Lisboa"), ("lisboa/lisboa", "council", "Lisboa")]
    if tokens and tokens[-1] == "Sintra":
        return "lisboa/sintra/colares", [("lisboa", "district", "Lisboa"), ("lisboa/sintra", "council", "Sintra"), ("lisboa/sintra/colares", "parish", "Colares")]
    return None, []


def test_ids_from_direct_links_tracking_redirects_and_text():
    assert mail.listing_id_from_url("https://www.idealista.pt/imovel/34567890/?utm_source=alert") == "34567890"
    assert mail.listing_id_from_url("https://click.idealista.pt/r?url=https%3A%2F%2Fwww.idealista.pt%2Fimovel%2F34567891%2F") == "34567891"
    assert mail.listing_id_from_url("https://www.idealista.pt/alertas/gerir") is None
    assert mail.listing_id_from_url(None) is None


def test_two_cards_from_the_fixture_with_fields_and_images():
    message = load_message()
    assert message.date == datetime(2026, 10, 28, 8, 15, 2, tzinfo=timezone.utc)
    cards = mail.cards_from_message(message)
    assert [c.idealista_id for c in cards] == ["34567890", "34567891"]
    flat, house = cards
    assert flat.title == "Apartamento T2 em Rua dos Remédios, Alfama, Lisboa"
    assert flat.images == [f"https://img4.idealista.pt/blur/WEB_LISTING/0/id.pro.pt.image.master/aa/bb/cc/{i}.jpg" for i in (1, 2, 3)]
    assert "pixel" not in " ".join(flat.images)
    assert house.title == "Moradia T3 em Colares, Sintra" and len(house.images) == 1


def test_canonical_listing_from_a_card():
    flat, house = mail.cards_from_message(load_message())
    l = mail.to_canonical(flat, received_at=datetime(2026, 10, 28, 8, 15, tzinfo=timezone.utc), resolve=fake_resolve)
    assert l.source_key == "idealista_email" and l.external_id == "34567890" and l.url == "https://www.idealista.pt/imovel/34567890/"
    assert l.price == 365_000 and l.rooms == 2 and l.area_util == 62.0 and l.floor == 2 and l.estate_type == "apartamento"
    assert l.geo_code == "lisboa/lisboa" and l.geo_path[-1][1] == "council"
    assert l.features["elevador"] == {"value": True, "tier": "confirmed"}   # "com elevador" is stated by the card
    assert l.features["varanda"]["tier"] == "mentioned" and l.features["vista"]["tier"] == "mentioned"
    assert "garagem" not in l.features and l.missing_fields() == []
    h = mail.to_canonical(house, received_at=None, resolve=fake_resolve)
    assert h.estate_type == "moradia" and h.rooms == 3 and h.price == 548_000 and h.area_util == 96.0 and h.floor is None
    assert h.features["garagem"]["tier"] == "mentioned" and "elevador" not in h.features
    assert h.geo_code == "lisboa/sintra/colares"


def test_absent_fields_stay_unknown_and_zone_falls_back():
    card = mail.Card("99999999", mail.canonical_url("99999999"), "Apartamento em Lisboa", "Preço sob consulta · sem elevador", [])
    l = mail.to_canonical(card, received_at=None, resolve=lambda t: ("lisboa", [("lisboa", "district", "Lisboa")]))
    assert l.price is None and l.rooms is None and l.area_util is None
    assert l.features["elevador"] == {"value": False, "tier": "absent"}
    assert set(l.missing_fields()) == {"preco", "area", "tipologia"} and l.geo_code == "lisboa"


def test_plain_text_fallback_when_there_is_no_html():
    class Msg:
        html = ""
        text = "Apartamento T1 em Benfica, Lisboa\n250.000 €\n45 m²\nhttps://www.idealista.pt/imovel/11112222/\n\nOutro texto"

    cards = mail.cards_from_message(Msg())
    assert len(cards) == 1 and cards[0].idealista_id == "11112222"
    l = mail.to_canonical(cards[0], received_at=None, resolve=lambda t: (None, []))
    assert l.price == 250_000 and l.rooms == 1 and l.area_util == 45.0 and "zona" in l.missing_fields()


def test_zone_tokens():
    assert mail.zone_tokens("Apartamento T2 em Rua dos Remédios, Alfama, Lisboa") == ["Rua dos Remédios", "Alfama", "Lisboa"]
    assert mail.zone_tokens("Moradia T3 em Colares, Sintra") == ["Colares", "Sintra"]
    assert mail.zone_tokens(None) == []
