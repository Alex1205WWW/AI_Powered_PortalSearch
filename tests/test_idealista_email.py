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


# --- real alert mail (captured 2026-10-30, recipient and tracking tokens scrubbed) ---------------
# The synthetic fixture above never showed the MJML layout Idealista actually sends. On the first real
# poll the parser recovered typology on 178 of 192 listings but price on 4 and the zone on 11. These
# tests pin the three causes so the regression cannot come back silently.

REAL_HTML = (FIX.parent / "idealista_alert_real.html").read_text(encoding="utf-8")
REAL_SUBJECT = (FIX.parent / "idealista_alert_real.subject.txt").read_text(encoding="utf-8").strip()


def _real_card():
    cards = mail.cards_from_html(REAL_HTML)
    assert len(cards) == 1, "this alert is about a single listing"
    return cards[0]


def test_real_card_reaches_the_price_in_a_sibling_column():
    """The photo link and the price sit in different table cells; the old 8-level climb never met them."""
    card = _real_card()
    text = f"{card.title or ''} {card.text}"
    assert mail.PRICE_RE.search(text), "the card root must cover the price block"
    assert card.images, "and the photos, which feed the dedup photo rule"


def test_real_card_takes_the_new_price_not_the_struck_one():
    """"O preço deste anúncio baixou de 735.000€ a 715.000€" - the first price in the card is the old one."""
    card = _real_card()
    text = f"{card.title or ''} {card.text}"
    assert mail.parse_price(text) == 715000
    assert mail.parse_previous_price(text) == 735000


def test_price_per_square_metre_is_never_read_as_the_price():
    assert mail.parse_price("715.000 € 6.391 €/m²") == 715000
    assert mail.parse_price("6.391 €/m²") is None


def test_previous_price_is_only_set_when_the_email_says_so():
    assert mail.parse_previous_price("715.000 € 6.391 €/m²") is None


def test_real_title_uses_na_not_em():
    card = _real_card()
    assert card.title == "Apartamento T3 na Alameda dos Pinheiros, Serra de Carnaxide, Venteira"
    assert mail.zone_tokens(card.title) == ["Alameda dos Pinheiros", "Serra de Carnaxide", "Venteira"]


def test_subject_carries_the_council():
    """Her saved searches are one per concelho and Idealista names it in the subject; the card names a
    street and a freguesia, which the static seed does not hold."""
    assert mail.subject_zone(REAL_SUBJECT) == "Amadora"
    assert mail.subject_zone("Novidades na tua pesquisa: Casas e apartamentos em Lisboa!") == "Lisboa"
    assert mail.subject_zone(None) is None


def test_real_card_fields():
    card = _real_card()
    text = f"{card.title or ''} {card.text}"
    assert mail.parse_typology(text) == 3
    assert mail.parse_area(text) == 115.0
    assert mail.parse_estate(text) == "apartamento"
