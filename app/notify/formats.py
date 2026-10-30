"""Telegram message bodies (HTML). One message per property; all matching buyers listed inside."""
from __future__ import annotations

from html import escape

from app.db.models import Listing, PropertyMatch
from app.domain.criteria import FEATURES
from app.domain.matching import tier_label

HEADERS = {
    "new": "🆕 Nova oportunidade",
    "price_drop": "📉 Baixa de preço",
    "data_completed": "✅ Requisito confirmado",
    "criteria_changed": "🔁 Nova correspondência (critérios alterados)",
}
SOURCE_NAMES = {"imovirtual": "Imovirtual", "remax": "RE/MAX", "idealista_email": "Idealista", "century21": "Century21"}


def source_label(key: str | None) -> str:
    """Display name for a link. Agency sources are created at runtime with the key `agency:<host>`, so they
    can never be in `SOURCE_NAMES`; showing the host is far more useful to her than a generic "Fonte"."""
    if not key:
        return "Fonte"
    if key in SOURCE_NAMES:
        return SOURCE_NAMES[key]
    if key.startswith("agency:"):
        return key.split(":", 1)[1].removeprefix("www.")
    return "Fonte"


def eur(value: int | None) -> str:
    return "preço sob consulta" if value is None else f"{value:,.0f} €".replace(",", ".")


def typology(rooms: int | None) -> str:
    return "T?" if rooms is None else ("T10+" if rooms >= 10 else f"T{rooms}")


def headline(listing: Listing) -> str:
    kind = (listing.estate_type or "imóvel").capitalize()
    area = f" · {listing.area_util or listing.area_bruta:.0f} m²" if (listing.area_util or listing.area_bruta) else ""
    return f"{kind} {typology(listing.rooms)} · {eur(None if listing.hide_price else listing.price)}{area}"


def property_alert(reason: str, primary: Listing, listings: list[Listing], buyers: list[tuple[str, PropertyMatch, list[str]]],
                   zone_name: str | None, source_keys: dict[int, str], old_price: int | None = None) -> str:
    lines = [f"<b>{HEADERS.get(reason, reason)}</b>", f"<b>{escape(headline(primary))}</b>"]
    if reason == "price_drop" and old_price:
        lines[-1] += f"  <s>{escape(eur(old_price))}</s>"
    if zone_name:
        lines.append(escape(zone_name) + (f" · {escape(primary.street)}" if primary.street else ""))
    for label, match, must_have in buyers:
        reqs = " · ".join(f"{FEATURES.get(k, k)}: {tier_label(primary.features, k)}" for k in must_have)
        grade = "confirmada" if match.grade == "confirmada" else "possível"
        lines.append(f"Para <b>{escape(label)}</b> ({grade})" + (f" — {escape(reqs)}" if reqs else ""))
    if primary.agency_name_raw:
        contact = escape(primary.agency_name_raw) + (f" · {escape(primary.agency_phone_raw)}" if primary.agency_phone_raw else "")
        lines.append(f"Mediadora: {contact}")
    if primary.is_private_owner:
        lines.append("Anúncio de particular")
    links = " · ".join(f'<a href="{escape(l.url)}">{escape(source_label(source_keys.get(l.source_id)))}</a>' for l in listings)
    lines.append(f"Ver: {links}")
    return "\n".join(lines)


def digest(date_label: str, per_buyer: list[tuple[str, int, int, int]]) -> str:
    lines = [f"<b>Resumo diário · {escape(date_label)}</b>"]
    for label, new, pending, drops in per_buyer:
        lines.append(f"• <b>{escape(label)}</b>: {new} novos · {pending} por contactar · {drops} baixas de preço")
    return "\n".join(lines)
