from __future__ import annotations

from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, or_, select

from app.db.models import Buyer, Criteria, Geo, Listing, PropertyMatch, Source
from app.domain.criteria import ESTATE_TYPES, STATES
from app.services import export
from app.services import matches as match_svc
from app.services import properties as prop_svc
from app.web.deps import DB, OptInt, OptStr, User, render

router = APIRouter()


def _listing_filters(stmt, *, zone, price_min, price_max, rooms_min, rooms_max, estate, q):
    """Narrow a `PropertyMatch` query by what its listings say.

    A property can have several listings and they do not always agree, so every condition means "**some**
    listing of this property satisfies it" - the same best-known-value logic matching uses. A property whose
    Imovirtual copy omits the price but whose RE/MAX copy has it must not vanish from a price filter."""
    if not any([zone, price_min, price_max, rooms_min, rooms_max, estate, q]):
        return stmt
    inner = select(Listing.property_id).where(Listing.property_id.is_not(None), Listing.status == "active")
    if zone:
        inner = inner.where(or_(Listing.geo_code == zone, Listing.geo_code.like(f"{zone}/%")))
    if price_min is not None:
        inner = inner.where(Listing.price >= price_min)
    if price_max is not None:
        inner = inner.where(Listing.price <= price_max)
    if rooms_min is not None:
        inner = inner.where(Listing.rooms >= rooms_min)
    if rooms_max is not None:
        inner = inner.where(Listing.rooms <= rooms_max)
    if estate:
        inner = inner.where(Listing.estate_type == estate)
    if q and q.strip():
        needle = f"%{q.strip()}%"
        inner = inner.where(or_(Listing.title.ilike(needle), Listing.street.ilike(needle),
                                Listing.agency_name_raw.ilike(needle)))
    return stmt.where(PropertyMatch.property_id.in_(inner))


def _zone_choices(db):
    """Councils that actually hold matched listings - offering all 50 would be mostly dead options."""
    codes = db.scalars(select(func.distinct(Listing.geo_code))
                       .where(Listing.geo_code.is_not(None), Listing.property_id.is_not(None)))
    councils = {"/".join(c.split("/")[:2]) for c in codes if c and "/" in c}
    if not councils:
        return []
    return [(g.code, g.name) for g in db.scalars(select(Geo).where(Geo.code.in_(councils)).order_by(Geo.name))]


@router.get("/properties")
async def list_properties(request: Request, db: DB, user: User, buyer: OptInt = None, state: OptStr = None,
                          grade: OptStr = None, zone: OptStr = None, price_min: OptInt = None,
                          price_max: OptInt = None, rooms_min: OptInt = None, rooms_max: OptInt = None,
                          estate: OptStr = None, q: OptStr = None, limit: int = 200):
    stmt = (
        select(PropertyMatch, Buyer.label, Criteria)
        .join(Buyer, Buyer.id == PropertyMatch.buyer_id)
        .join(Criteria, Criteria.id == PropertyMatch.criteria_id)
        .where(PropertyMatch.grade != "fora")
        .order_by(PropertyMatch.matched_at.desc())
    )
    if buyer:
        stmt = stmt.where(PropertyMatch.buyer_id == buyer)
    if state:
        stmt = stmt.where(PropertyMatch.state == state)
    if grade:
        stmt = stmt.where(PropertyMatch.grade == grade)
    stmt = _listing_filters(stmt, zone=zone, price_min=price_min, price_max=price_max, rooms_min=rooms_min,
                            rooms_max=rooms_max, estate=estate, q=q)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.limit(limit)).all()

    property_ids = {m.property_id for m, _, _ in rows}
    listings_by_property: dict[int, list[Listing]] = {}
    if property_ids:
        for listing in db.scalars(select(Listing).where(Listing.property_id.in_(property_ids)).order_by(Listing.first_seen_at)):
            listings_by_property.setdefault(listing.property_id, []).append(listing)
    geo_codes = {l.geo_code for ls in listings_by_property.values() for l in ls if l.geo_code}
    geo_names = {g.code: g.name for g in db.scalars(select(Geo).where(Geo.code.in_(geo_codes)))} if geo_codes else {}
    sources_by_id = {s.id: s for s in db.scalars(select(Source))}
    source_names = {sid: s.name for sid, s in sources_by_id.items()}

    items = []
    for match, label, criteria in rows:
        listings = listings_by_property.get(match.property_id, [])
        primary = next((l for l in listings if l.status == "active"), listings[0] if listings else None)
        if primary is None:
            continue
        # The requirements column must read the MERGED view, not the primary listing. Matching grades a
        # property on everything known about it across its sources, so when Imovirtual is silent about the
        # terrace and RE/MAX confirms it, the grade is `confirmada` - and showing the primary listing's
        # silence made 57 of Rosa's 144 confirmadas read "confirmada ... exterior: nao indicado", which is
        # exactly the contradiction that destroys trust in the grade. Found 2026-10-31 from a screenshot.
        view = prop_svc.build_view(match.property_id, listings, sources_by_id)
        items.append({"match": match, "buyer_label": label, "criteria": criteria, "primary": primary,
                      "listings": listings, "features": (view.features if view is not None else primary.features),
                      "zone": geo_names.get(primary.geo_code, primary.geo_code)})
    buyers = list(db.scalars(select(Buyer).order_by(Buyer.label)))
    filters = {"buyer": buyer, "state": state, "grade": grade, "zone": zone, "price_min": price_min,
               "price_max": price_max, "rooms_min": rooms_min, "rooms_max": rooms_max, "estate": estate, "q": q}
    active = {k: v for k, v in filters.items() if v not in (None, "")}
    query = urlencode(active)
    return render(request, "properties.html", items=items, buyers=buyers, states=STATES, source_names=source_names,
                  filters=filters, count=len(items), total=total, zones=_zone_choices(db),
                  estate_types=ESTATE_TYPES, narrowed=len(active),
                  export_query=f"?{query}" if query else "")


@router.get("/properties/export.xlsx")
async def export_properties(request: Request, db: DB, user: User, buyer: OptInt = None,
                            state: OptStr = None, grade: OptStr = None):
    """The same set the panel is showing, as a spreadsheet (deliverable 2)."""
    label = db.scalar(select(Buyer.label).where(Buyer.id == buyer)) if buyer else None
    stream = export.workbook(db, buyer_id=buyer, state=state, grade=grade)
    headers = {"Content-Disposition": f'attachment; filename="{export.filename(label)}"'}
    return StreamingResponse(stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers=headers)


@router.post("/matches/{match_id}/state")
async def set_state(request: Request, db: DB, user: User, match_id: int, state: Annotated[str, Form()],
                    commission_share_pct: Annotated[str | None, Form()] = None, back: Annotated[str, Form()] = "/properties"):
    share = None
    if commission_share_pct and commission_share_pct.strip():
        try:
            share = float(commission_share_pct.replace(",", "."))
        except ValueError:
            share = None
    try:
        match_svc.set_state(db, match_id, state, commission_share_pct=share)
        db.commit()
    except ValueError as exc:
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg={quote(str(exc))}", status_code=303)
    target = back if back.startswith("/") and not back.startswith("//") else "/properties"
    return RedirectResponse(target, status_code=303)


@router.post("/listings/{listing_id}/split")
async def split_listing(request: Request, db: DB, user: User, listing_id: int, back: Annotated[str, Form()] = "/properties"):
    """Operator override: this listing is not the same property as the others in its cluster."""
    target = back if back.startswith("/") and not back.startswith("//") else "/properties"
    try:
        prop_svc.split(db, listing_id)
        db.commit()
    except ValueError as exc:
        return RedirectResponse(f"{target}{'&' if '?' in target else '?'}msg={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"{target}{'&' if '?' in target else '?'}msg={quote('Anúncio separado. Volta a aparecer como imóvel próprio na próxima correspondência.')}", status_code=303)


def back_url(filters: dict) -> str:
    query = urlencode({k: v for k, v in filters.items() if v})
    return "/properties" + (f"?{query}" if query else "")
