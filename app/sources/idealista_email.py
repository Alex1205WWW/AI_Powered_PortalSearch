"""Idealista saved-search alert emails, read from her dedicated mailbox over IMAP (Roadmap_M1.md §7).

There is no automated access to idealista.pt (robots.txt answers 403); the alert emails Idealista sends to her own
mailbox are the only agreed channel. Each email carries one or more listing "cards": a link to
`idealista.pt/imovel/<id>/`, the title ("Apartamento T2 em Alfama, Lisboa"), the price, the area and a floor line
that often states "com elevador" / "sem elevador". Everything the card does not say stays `unknown`
(confidence `partial`, shown as "dados parciais" in the panel).

Mechanics: `imap-tools`, UID watermark (`app_settings`), `UIDVALIDITY` guard, nothing marked as read, re-delivered
emails are idempotent because listings upsert on the Idealista id and notifications are DB-unique.

Layout facts are validated on a synthetic fixture until the first real alert arrives in her mailbox; link extraction
is the robust part, field extraction is re-checked then (Roadmap_M1.md §8.3).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node
from sqlalchemy.orm import Session

from app.domain.listing import CanonicalListing, features_from_text, merge_features, normalize_text
from app.geo import resolve as geo

log = logging.getLogger(__name__)

SOURCE_KEY = "idealista_email"
ID_RE = re.compile(r"idealista\.(?:pt|com)/(?:[a-z]{2}/)?imovel/(\d{5,12})\b", re.I)
_AMOUNT = r"\d{1,3}(?:[.\s\u00a0]\d{3})+|\d{4,7}"
# A price, but never the EUR/m2 figure that sits next to it in every card.
PRICE_RE = re.compile(rf"({_AMOUNT})\s*€(?!\s*/\s*m)")
# "O preço deste anúncio baixou de 735.000€ a 715.000€" -> (previous, current)
PRICE_DROP_RE = re.compile(rf"baixou\s+de\s*({_AMOUNT})\s*€\s*(?:a|para)\s*({_AMOUNT})\s*€", re.I)
TYPOLOGY_RE = re.compile(r"\bT(\d{1,2})\b")
AREA_RE = re.compile(r"(\d{2,5}(?:[.,]\d+)?)\s*m(?:²|2)\b")
FLOOR_RE = re.compile(r"(\d{1,2})\s*[.ºo°]*\s*andar", re.I)
GROUND_RE = re.compile(r"r[ée]s[- ]do[- ]ch[ãa]o|\br/c\b", re.I)
ESTATE_WORDS = {"apartamento": "apartamento", "moradia": "moradia", "casa": "moradia", "terreno": "terreno", "estudio": "apartamento",
                "duplex": "apartamento", "penthouse": "apartamento", "quinta": "moradia", "vivenda": "moradia"}
TRACKING_HINTS = ("pixel", "track", "open", "beacon", "spacer", "blank")
BLOCK_TAGS = {"table", "tr", "li", "article", "section"}
MAX_CARD_DEPTH = 20  # MJML nests deeply; the real boundary is "how many listings does this node cover"


@dataclass
class Card:
    idealista_id: str
    url: str
    title: str | None
    text: str
    images: list[str] = field(default_factory=list)


# --- extraction ----------------------------------------------------------------------------------

def listing_id_from_url(href: str | None) -> str | None:
    """Idealista id from a direct link or from a tracking redirect that carries the target URL in its query."""
    if not href:
        return None
    match = ID_RE.search(unquote(href))
    if match:
        return match.group(1)
    for values in parse_qs(urlparse(href).query).values():
        for value in values:
            match = ID_RE.search(unquote(value))
            if match:
                return match.group(1)
    return None


def canonical_url(idealista_id: str) -> str:
    return f"https://www.idealista.pt/imovel/{idealista_id}/"


def _is_tracking_pixel(img: Node) -> bool:
    src = (img.attributes.get("src") or "").lower()
    if any(hint in src for hint in TRACKING_HINTS):
        return True
    for attr in ("width", "height"):
        value = (img.attributes.get(attr) or "").strip().rstrip("px")
        if value.isdigit() and int(value) <= 2:
            return True
    return False


def _ids_in(node: Node) -> set[str]:
    """Every distinct listing id linked from inside this node."""
    found = set()
    for link in node.css("a[href]"):
        listing_id = listing_id_from_url(link.attributes.get("href"))
        if listing_id:
            found.add(listing_id)
    return found


def _card_root(anchor: Node) -> Node:
    """Climb from the link to the largest ancestor that still describes only this one listing.

    Idealista's emails are MJML: the photo, the title and the price sit in sibling table cells, so the
    smallest ancestor holding all three is a long way above the anchor (measured on real mail 2026-10-30:
    further than the eight levels this used to climb, which is why price came back empty on 188 of 192
    listings). Climbing by "how many listings does this node cover" is the honest boundary - we take the
    highest node that still covers exactly one, and prefer the first such node that also shows a price."""
    node, best, priced = anchor, anchor, None
    for _ in range(MAX_CARD_DEPTH):
        parent = node.parent
        if parent is None or parent.tag in ("body", "html", "[document]"):
            break
        if len(_ids_in(parent)) > 1:
            break  # one more step would merge this card with the next listing
        node = best = parent
        if priced is None and PRICE_RE.search(node.text(separator=" ", strip=True) or ""):
            priced = node
    return priced or best


def cards_from_html(html: str) -> list[Card]:
    tree = HTMLParser(html)
    by_id: dict[str, Card] = {}
    for anchor in tree.css("a[href]"):
        idealista_id = listing_id_from_url(anchor.attributes.get("href"))
        if not idealista_id:
            continue
        root = _card_root(anchor)
        text = re.sub(r"\s+", " ", root.text(separator=" ", strip=True) or "").strip()
        images = []
        for img in root.css("img[src]"):
            src = img.attributes.get("src") or ""
            if src.startswith("http") and not _is_tracking_pixel(img) and src not in images:
                images.append(src)
        label = re.sub(r"\s+", " ", anchor.text(strip=True) or "").strip()
        card = by_id.get(idealista_id)
        if card is None:
            by_id[idealista_id] = Card(idealista_id, canonical_url(idealista_id), label or None, text, images[:3])
        else:
            if len(text) > len(card.text):
                card.text = text
            if label and (card.title is None or len(label) > len(card.title)):
                card.title = label
            for src in images:
                if src not in card.images and len(card.images) < 3:
                    card.images.append(src)
    return list(by_id.values())


def cards_from_text(text: str) -> list[Card]:
    """Plain-text fallback: links only, the surrounding paragraph as card text."""
    cards: dict[str, Card] = {}
    for paragraph in re.split(r"\n\s*\n", text or ""):
        for match in ID_RE.finditer(paragraph):
            idealista_id = match.group(1)
            if idealista_id not in cards:
                cards[idealista_id] = Card(idealista_id, canonical_url(idealista_id), None, re.sub(r"\s+", " ", paragraph).strip(), [])
    return list(cards.values())


def cards_from_message(message) -> list[Card]:
    """`message` is an imap_tools.MailMessage (or anything with `.html` / `.text`)."""
    cards = cards_from_html(message.html) if message.html else []
    return cards or cards_from_text(message.text or "")


# --- field parsing -------------------------------------------------------------------------------

def _amount(raw: str) -> int:
    return int(re.sub(r"[.\s\u00a0]", "", raw))


def parse_price(text: str) -> int | None:
    """The price the listing is on the market for *now*."""
    drop = PRICE_DROP_RE.search(text)
    if drop:
        return _amount(drop.group(2))
    match = PRICE_RE.search(text)
    return _amount(match.group(1)) if match else None


def parse_previous_price(text: str) -> int | None:
    """Only when the email states a reduction outright; never guessed from two prices being present."""
    drop = PRICE_DROP_RE.search(text)
    return _amount(drop.group(1)) if drop else None


def parse_typology(text: str) -> int | None:
    match = TYPOLOGY_RE.search(text)
    return int(match.group(1)) if match else None


def parse_area(text: str) -> float | None:
    match = AREA_RE.search(text)
    return float(match.group(1).replace(",", ".")) if match else None


def parse_floor(text: str) -> int | None:
    if GROUND_RE.search(text):
        return 0
    match = FLOOR_RE.search(text)
    return int(match.group(1)) if match else None


def parse_estate(text: str) -> str | None:
    norm = normalize_text(text)
    for word, estate in ESTATE_WORDS.items():
        if re.search(rf"\b{word}s?\b", norm):
            return estate
    return None


def parse_elevator(text: str) -> bool | None:
    norm = normalize_text(text)
    if re.search(r"\bcom elevador", norm):
        return True
    if re.search(r"\bsem elevador", norm):
        return False
    return None


def zone_tokens(title: str | None) -> list[str]:
    """"Apartamento T3 na Alameda dos Pinheiros, Serra de Carnaxide, Venteira"
    -> ["Alameda dos Pinheiros", "Serra de Carnaxide", "Venteira"].

    Real titles use "na" / "no" far more often than "em" (the original pattern only knew "em", which left
    the property type glued to the street and no token that could resolve). The right-most token is the
    narrowest zone Idealista states, which is usually a freguesia, not the município."""
    if not title:
        return []
    match = re.search(r"\b(?:em|na|no|nas|nos)\s+(.+)$", unescape(title), re.I)
    tail = match.group(1) if match else title
    return [t.strip(" .") for t in tail.split(",") if t.strip(" .")]


def subject_zones(subject: str | None) -> list[str]:
    """Place names from the alert subject, in the order the subject gives them.

    The subject is the dependable locality in an alert email: each saved search covers one area and
    Idealista names it after the colon, while the card title only gives a street and a freguesia.
    Three real shapes (2026-10-30):
        "Desceu o preco na tua pesquisa: Casas e apartamentos na Amadora!"  -> ["Amadora"]
        "Novo apartamento T3 na tua pesquisa: Odivelas!"                    -> ["Odivelas"]
        "Novo apartamento T2 na tua pesquisa: Lumiar - Santa Clara - Carnide - Benfica!"
                                       -> ["Lumiar", "Santa Clara", "Carnide", "Benfica"]
    The second shape is why this cannot require a preposition, and the third is her own multi-freguesia
    search, where the subject names several zones and none of them a concelho. Every candidate is
    returned; the resolver takes the first that exists."""
    if not subject:
        return []
    tail = re.sub(r"\s+", " ", unescape(subject)).split(":")[-1]
    tail = re.sub(r"^.*?\b(?:em|na|no|nas|nos)\s+", "", tail, flags=re.I)  # drop "Casas e apartamentos na "
    parts = [part.strip(" .!") for part in re.split(r"\s+-\s+|,", tail)]
    return [part for part in parts
            if part and not re.search(r"\b(pesquisa|apartamentos?|casas?|moradias?|im[oó]ve)", part, re.I)]


def subject_zone(subject: str | None) -> str | None:
    """The first place the subject names."""
    zones = subject_zones(subject)
    return zones[0] if zones else None


def resolve_zone(session: Session, tokens: list[str]) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Right-to-left: the município first (council level), then a parish under it if the card names one.
    Ambiguous names ("Lisboa" district vs council) resolve to the council: alert cards always name the município."""
    council = None
    for token in reversed(tokens):
        hits = geo.find(session, token, level="council")
        if hits:
            council = hits[0]
            break
    if council is None:
        for token in reversed(tokens):
            hits = geo.find(session, token, level="district")
            if hits:
                return hits[0].code, [(hits[0].code, "district", hits[0].name)]
        # Her custom saved search is named after freguesias ("Lumiar - Santa Clara - Carnide - Benfica"),
        # so a subject can name a parish and nothing above it. Accept one only when the name is unique in
        # the three districts: "São Vicente" exists in several councils and guessing would put a listing
        # in the wrong zone, which is worse than leaving it unresolved.
        for token in reversed(tokens):
            hits = geo.find(session, token, level="parish")
            if len(hits) == 1:
                parish = hits[0]
                council_row = session.get(geo.Geo, parish.parent_code) if parish.parent_code else None
                district_row = (session.get(geo.Geo, council_row.parent_code)
                                if council_row and council_row.parent_code else None)
                path = []
                if district_row:
                    path.append((district_row.code, "district", district_row.name))
                if council_row:
                    path.append((council_row.code, "council", council_row.name))
                path.append((parish.code, "parish", parish.name))
                return parish.code, path
        return None, []
    district_code = council.parent_code
    district = session.get(geo.Geo, district_code) if district_code else None
    path = ([(district.code, "district", district.name)] if district else []) + [(council.code, "council", council.name)]
    for token in reversed(tokens):
        for parish in geo.find(session, token, level="parish"):
            if parish.parent_code == council.code:
                return parish.code, path + [(parish.code, "parish", parish.name)]
    return council.code, path


def to_canonical(card: Card, received_at: datetime | None,
                 resolve: Callable[[list[str]], tuple[str | None, list[tuple[str, str, str]]]],
                 subject: str | None = None) -> CanonicalListing:
    text = f"{card.title or ''} {card.text}"
    geo_code, geo_path = resolve(zone_tokens(card.title))
    if geo_code is None:
        # The card named a street and a freguesia we do not know yet; the subject names the search area.
        for candidate in subject_zones(subject):
            geo_code, geo_path = resolve([candidate])
            if geo_code:
                break
    typed: dict[str, bool | None] = {"elevador": parse_elevator(text)}
    previous_price = parse_previous_price(text)
    return CanonicalListing(
        source_key=SOURCE_KEY,
        external_id=card.idealista_id,
        url=card.url,
        title=card.title,
        price=parse_price(text),
        previous_price=previous_price,
        price_reduced_at=received_at if previous_price else None,
        area_util=parse_area(card.text),
        rooms=parse_typology(text),
        floor=parse_floor(card.text),
        estate_type=parse_estate(text),
        geo_code=geo_code,
        geo_path=geo_path,
        features=merge_features(typed, features_from_text(card.title, card.text)),
        images=card.images[:3],
        description=card.text[:2000] or None,
        published_at=received_at,
        raw={"title": card.title, "text": card.text[:4000], "images": card.images, "subject": subject},
    )


# --- IMAP -----------------------------------------------------------------------------------------

def connect(settings):
    from imap_tools import MailBox

    return MailBox(settings.imap_host).login(settings.imap_user, settings.imap_password, initial_folder=settings.imap_folder)


def uidvalidity(box, folder: str) -> int | None:
    try:
        return int(box.folder.status(folder, ["UIDVALIDITY"]).get("UIDVALIDITY"))
    except Exception as exc:  # noqa: BLE001 - some servers refuse STATUS on the selected folder
        log.debug("imap STATUS failed: %s", exc)
        return None


def fetch_after(box, last_uid: int) -> Iterator:
    """Messages with UID > last_uid, oldest first. IMAP's `n:*` always includes the last message, so filter again."""
    from imap_tools import AND, UidRange

    for message in box.fetch(AND(uid=UidRange(last_uid + 1, "*")), mark_seen=False, bulk=True):
        if int(message.uid) > last_uid:
            yield message
