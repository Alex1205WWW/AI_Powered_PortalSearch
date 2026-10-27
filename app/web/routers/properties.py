from __future__ import annotations

from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.db.models import Buyer, Criteria, Geo, Listing, PropertyMatch, Source
from app.domain.criteria import STATES
from app.services import matches as match_svc
from app.web.deps import DB, User, render

router = APIRouter()


@router.get("/properties")
async def list_properties(request: Request, db: DB, user: User, buyer: int | None = None, state: str | None = None,
                          grade: str | None = None, limit: int = 200):
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
    rows = db.execute(stmt.limit(limit)).all()

    property_ids = {m.property_id for m, _, _ in rows}
    listings_by_property: dict[int, list[Listing]] = {}
    if property_ids:
        for listing in db.scalars(select(Listing).where(Listing.property_id.in_(property_ids)).order_by(Listing.first_seen_at)):
            listings_by_property.setdefault(listing.property_id, []).append(listing)
    geo_codes = {l.geo_code for ls in listings_by_property.values() for l in ls if l.geo_code}
    geo_names = {g.code: g.name for g in db.scalars(select(Geo).where(Geo.code.in_(geo_codes)))} if geo_codes else {}
    source_names = {s.id: s.name for s in db.scalars(select(Source))}

    items = []
    for match, label, criteria in rows:
        listings = listings_by_property.get(match.property_id, [])
        primary = next((l for l in listings if l.status == "active"), listings[0] if listings else None)
        if primary is None:
            continue
        items.append({"match": match, "buyer_label": label, "criteria": criteria, "primary": primary, "listings": listings,
                      "zone": geo_names.get(primary.geo_code, primary.geo_code)})
    buyers = list(db.scalars(select(Buyer).order_by(Buyer.label)))
    return render(request, "properties.html", items=items, buyers=buyers, states=STATES, source_names=source_names,
                  filters={"buyer": buyer, "state": state, "grade": grade}, count=len(items))


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


def back_url(filters: dict) -> str:
    query = urlencode({k: v for k, v in filters.items() if v})
    return "/properties" + (f"?{query}" if query else "")
