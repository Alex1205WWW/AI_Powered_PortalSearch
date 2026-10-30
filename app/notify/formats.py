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


# --- Day 6: the confirmation card, the state keyboard, the digest -------------------------------
# `callback_data` is capped at 64 bytes by Telegram, so the payloads stay short and opaque:
#   d:<draft_id>:ok|fix|no          the confirmation card
#   b:<draft_id>:<buyer_id>         "this is that buyer, not a new one"
#   m:<match_id>:<state>            a state button on an alert

STATE_LABELS = {"analisado": "Analisado", "contactado": "Contactado",
                "disponivel": "Disponível", "indisponivel": "Indisponível"}
STATE_MARKS = {"novo": "🆕", "analisado": "👀", "contactado": "📞", "disponivel": "✅", "indisponivel": "❌"}
ESTATE_LABELS = {"apartamento": "Apartamento", "moradia": "Moradia", "terreno": "Terreno", "outro": "Outro"}


def criteria_card(parsed: dict, ambiguities: list[str], zone_names: list[str]) -> str:
    """What the system understood, in her words, before anything is created.

    Everything uncertain is shown as a question. A card that is wrong must look wrong - that is the whole
    point of the confirmation step."""
    from app.domain.criteria import price_label, typology_label

    buyer = parsed.get("buyer_label") or "—"
    lines = ["<b>Confirma esta pesquisa?</b>", f"Comprador: <b>{escape(str(buyer))}</b>"]
    kinds = ", ".join(ESTATE_LABELS.get(t, t) for t in (parsed.get("estate_types") or [])) or "apartamento"
    lines.append(f"{escape(kinds)} · {escape(typology_label(parsed.get('typology_min'), parsed.get('typology_max')))}")
    lines.append(escape(price_label(parsed.get("price_min"), parsed.get("price_max"))))
    if parsed.get("area_min"):
        lines.append(f"Área mínima: {parsed['area_min']} m²")
    lines.append("Zonas: " + (escape(", ".join(zone_names)) if zone_names else "<i>por definir</i>"))
    if parsed.get("must_have"):
        lines.append("Obrigatório: " + escape(", ".join(FEATURES.get(f, f) for f in parsed["must_have"])))
    if parsed.get("nice_to_have"):
        lines.append("Desejável: " + escape(", ".join(FEATURES.get(f, f) for f in parsed["nice_to_have"])))
    if parsed.get("exclude"):
        lines.append("Excluir: " + escape(", ".join(parsed["exclude"])))
    lines.append("Só compra." if (parsed.get("transaction") or "sale") == "sale" else "Arrendamento.")
    if ambiguities:
        lines.append("")
        lines.append("<b>Preciso de confirmar:</b>")
        lines += [f"• {escape(q)}" for q in ambiguities]
    return "\n".join(lines)



def confirmed_text(buyer_label: str, panel_url: str | None, queued: bool = True) -> str:
    lines = [f"Pesquisa criada para <b>{escape(buyer_label)}</b>."]
    if queued:
        lines.append("Estou a fazer a primeira busca completa. <b>O resultado aparece no painel</b>, não aqui — "
                     "pode ser muita coisa. Depois disso, só lhe mando as novidades.")
    if panel_url:
        lines.append(f'<a href="{escape(panel_url)}">Abrir o painel</a>')
    return "\n".join(lines)


def digest(date_label: str, per_buyer: list[tuple[str, int, int, int]], degraded_agencies: int = 0,
           panel_url: str | None = None) -> str:
    """Daily summary. Deliberately quiet when there is nothing: she should be able to trust that a digest
    with zeros means zero, not that something broke silently."""
    lines = [f"<b>Resumo de {escape(date_label)}</b>"]
    if not per_buyer:
        lines.append("Sem compradores ativos.")
    total = 0
    for label, new_count, confirmed, possible in per_buyer:
        total += new_count
        if new_count:
            lines.append(f"<b>{escape(label)}</b>: {new_count} novo(s) · {confirmed} confirmada(s) · {possible} possível(eis)")
        else:
            lines.append(f"{escape(label)}: sem novidades")
    if total == 0 and per_buyer:
        lines.append("")
        lines.append("<i>Nada novo nas últimas 24 horas. As fontes estão a funcionar.</i>")
    if degraded_agencies:
        word = "mediadora" if degraded_agencies == 1 else "mediadoras"
        lines.append(f"⚠️ {degraded_agencies} {word} com problemas de leitura — ver o painel.")
    if panel_url:
        lines.append(f'<a href="{escape(panel_url)}">Abrir o painel</a>')
    return "\n".join(lines)


def state_changed(match_state: str, buyer_label: str) -> str:
    return f"{STATE_MARKS.get(match_state, '')} {escape(buyer_label)}: {STATE_LABELS.get(match_state, match_state)}"


# --- inline keyboards --------------------------------------------------------------------------

def draft_keyboard(draft_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Confirmar", "callback_data": f"d:{draft_id}:ok"},
        {"text": "✏️ Corrigir", "callback_data": f"d:{draft_id}:fix"},
        {"text": "🗑 Cancelar", "callback_data": f"d:{draft_id}:no"},
    ]]}


def buyer_choice_keyboard(draft_id: int, candidates: list[tuple[int, str]]) -> dict:
    """"Is this the Ana Santos you already have, or a new buyer?" - asked before a duplicate is created,
    because two buyers with the same name silently split her results in half."""
    rows = [[{"text": f"👤 {label}", "callback_data": f"b:{draft_id}:{buyer_id}"}] for buyer_id, label in candidates[:4]]
    rows.append([{"text": "➕ É um comprador novo", "callback_data": f"b:{draft_id}:0"}])
    return {"inline_keyboard": rows}


def state_keyboard(matches: list[tuple[int, str]], current: dict[int, str] | None = None) -> dict:
    """One row per buyer: `[Rosa: 👀] [📞] [✅] [❌]`.

    With a single buyer the labels are spelled out. With several, the first button of each row carries the
    buyer's first name, so every button is live and she can still tell whose row she is tapping - an alert
    can cover two buyers, and an unlabelled second row would be a guess.

    `current` marks the state each match is already in with a bullet, so after she taps the message shows
    where things stand instead of looking unchanged."""
    single = len(matches) == 1
    marked = current or {}
    rows = []
    for match_id, label in matches:
        first = label.split()[0] if label else "?"
        buttons = []
        for index, (state, text) in enumerate(STATE_LABELS.items()):
            mark = STATE_MARKS.get(state, "")
            caption = f"{mark} {text}" if single else (f"{first}: {mark}" if index == 0 else mark)
            if marked.get(match_id) == state:
                caption = f"• {caption}"
            buttons.append({"text": caption, "callback_data": f"m:{match_id}:{state}"})
        rows.append(buttons)
    return {"inline_keyboard": rows}
