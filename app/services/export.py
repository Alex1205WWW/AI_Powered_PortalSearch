"""Excel export of the results a buyer sees in the panel (deliverable 2, Roadmap_M1.md §6 Day 4).

Written with openpyxl so she can open it in Excel and sort or filter it herself. Numbers are written as
numbers (not text), so "ordenar por preço" works; accents survive because xlsx is UTF-8 throughout.
The sheet is deliberately the same set and the same order as the panel: an export that disagrees with the
screen is worse than no export.
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Buyer, Criteria, Geo, Listing, ListingPriceHistory, PropertyMatch, Source
from app.domain.criteria import FEATURES
from app.domain.matching import tier_label

COLUMNS = [
    ("Comprador", 22), ("Estado", 13), ("Grau", 11), ("Tipo", 13), ("Tipologia", 10),
    ("Preço (€)", 13), ("Preço anterior (€)", 17), ("Área (m²)", 11), ("Zona", 28),
    ("Requisitos", 34), ("Mediadora", 24), ("Telefone", 17), ("Fontes", 20),
    ("Primeira vez", 17), ("Visto em", 17), ("Link", 60), ("Partilha (%)", 12), ("Notas", 30),
]
GRADE_LABEL = {"confirmada": "confirmada", "possivel": "possível", "fora": "fora"}


def _requirements(criteria: Criteria, features: dict | None) -> str:
    """The same wording as the panel: confirmado / mencionado no texto / não indicado / não tem."""
    parts = [f"{FEATURES.get(key, key)}: {tier_label(features, key)}" for key in (criteria.must_have or [])]
    parts += [f"(+) {FEATURES.get(key, key)}: {tier_label(features, key)}" for key in (criteria.nice_to_have or [])]
    return " · ".join(parts)


def _naive(value: datetime | None) -> datetime | None:
    """Excel cannot store a timezone-aware datetime."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def rows_for(session: Session, *, buyer_id: int | None = None, state: str | None = None,
             grade: str | None = None, limit: int = 5000) -> list[list]:
    stmt = (
        select(PropertyMatch, Buyer.label, Criteria)
        .join(Buyer, Buyer.id == PropertyMatch.buyer_id)
        .join(Criteria, Criteria.id == PropertyMatch.criteria_id)
        .where(PropertyMatch.grade != "fora")
        .order_by(PropertyMatch.matched_at.desc())
    )
    if buyer_id:
        stmt = stmt.where(PropertyMatch.buyer_id == buyer_id)
    if state:
        stmt = stmt.where(PropertyMatch.state == state)
    if grade:
        stmt = stmt.where(PropertyMatch.grade == grade)
    matches = session.execute(stmt.limit(limit)).all()

    property_ids = {m.property_id for m, _, _ in matches}
    by_property: dict[int, list[Listing]] = {}
    if property_ids:
        for listing in session.scalars(select(Listing).where(Listing.property_id.in_(property_ids)).order_by(Listing.first_seen_at)):
            by_property.setdefault(listing.property_id, []).append(listing)
    source_names = {s.id: s.name for s in session.scalars(select(Source))}
    geo_codes = {l.geo_code for ls in by_property.values() for l in ls if l.geo_code}
    geo = {g.code: g for g in session.scalars(select(Geo).where(Geo.code.in_(geo_codes)))} if geo_codes else {}

    def zone_name(code: str | None) -> str:
        node = geo.get(code or "")
        if node is None:
            return code or ""
        parent = geo.get(node.parent_code or "")
        return f"{node.name}, {parent.name}" if parent and node.level == "parish" else node.name

    out: list[list] = []
    for match, label, criteria in matches:
        listings = by_property.get(match.property_id, [])
        primary = next((l for l in listings if l.status == "active"), listings[0] if listings else None)
        if primary is None:
            continue
        previous = session.scalar(
            select(ListingPriceHistory.price).where(ListingPriceHistory.listing_id == primary.id)
            .order_by(ListingPriceHistory.observed_at.desc()).offset(1).limit(1))
        contact = next((l for l in listings if l.agency_phone_raw), primary)
        out.append([
            label,
            match.state,
            GRADE_LABEL.get(match.grade, match.grade or ""),
            primary.estate_type or "",
            "" if primary.rooms is None else f"T{primary.rooms}",
            None if primary.hide_price else primary.price,
            previous if (previous is not None and previous != primary.price) else None,
            primary.area_util or primary.area_bruta,
            zone_name(primary.geo_code),
            _requirements(criteria, primary.features),
            primary.agency_name_raw or "",
            contact.agency_phone_raw or "",
            ", ".join(sorted({source_names.get(l.source_id, "?") for l in listings})),
            _naive(primary.first_seen_at),
            _naive(primary.last_seen_at),
            primary.url,
            match.commission_share_pct,
            match.notes or "",
        ])
    return out


def workbook(session: Session, **filters) -> BytesIO:
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Imóveis"
    sheet.append([title for title, _ in COLUMNS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center")
    for row in rows_for(session, **filters):
        sheet.append(row)
    for index, (_, width) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for column, fmt in (("F", "#,##0"), ("G", "#,##0"), ("H", "#,##0.0"),
                        ("N", "dd/mm/yyyy hh:mm"), ("O", "dd/mm/yyyy hh:mm"), ("Q", "0.0")):
        for cell in sheet[column][1:]:
            cell.number_format = fmt
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{sheet.max_row}"
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def filename(buyer_label: str | None = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    who = "".join(c for c in (buyer_label or "todos") if c.isalnum() or c in " -_").strip().replace(" ", "-")
    return f"imoveis-{who.lower()}-{stamp}.xlsx"
