"""Mediadoras: the panel page for agency discovery (deliverables 4, 5).

Separate from /sources on purpose. There can be hundreds of agency sites, so this page is paginated and
filtered, while /sources stays the short list of portals she recognises.
"""
from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app.db.models import Agency, Source
from app.services import agencies as svc
from app.web.deps import DB, OptInt, OptStr, User, render

router = APIRouter(prefix="/agencies")
PAGE_SIZE = 50
FILTERS = ("legiveis", "com-site", "sem-site", "problemas", "todas")


@router.get("")
def list_agencies(request: Request, db: DB, user: User, page: OptInt = None, f: OptStr = None, q: OptStr = None):
    page = max(1, page or 1)
    f = f or "legiveis"
    q = q or ""
    stmt = select(Agency, Source).outerjoin(Source, Source.id == Agency.watch_source_id)
    if f == "legiveis":
        stmt = stmt.where(Source.confidence.in_(("full", "partial")))
    elif f == "com-site":
        stmt = stmt.where(Agency.website_url.is_not(None))
    elif f == "sem-site":
        stmt = stmt.where(Agency.website_url.is_(None))
    elif f == "problemas":
        stmt = stmt.where(Source.status.in_(("degraded", "blocked")))
    if q:
        stmt = stmt.where(or_(Agency.name.ilike(f"%{q}%"), Agency.website_url.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Agency.offportal_ratio.desc().nullslast(), Agency.active_ads_count.desc().nullslast())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return render(request, "agencies.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
                  summary=svc.summary(db), f=f if f in FILTERS else "legiveis", q=q, filters=FILTERS,
                  offportal_min=svc.OFFPORTAL_MIN_SAMPLE)


@router.post("/add")
def add(request: Request, db: DB, user: User, website_url: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = ""):
    if not website_url.strip():
        return RedirectResponse(f"/agencies?msg={quote('Indique o endereço do site da mediadora.')}", status_code=303)
    try:
        agency = svc.add_manual_website(db, name.strip() or website_url, website_url.strip())
        svc.ensure_source(db, agency)
        db.commit()
        message = f"{agency.name} adicionada. Será perfilada na próxima passagem."
    except Exception as exc:  # noqa: BLE001 - a bad paste must not 500 the page
        db.rollback()
        message = f"Não foi possível adicionar: {exc}"
    return RedirectResponse(f"/agencies?msg={quote(message)}", status_code=303)


@router.post("/{source_id}/toggle")
def toggle(request: Request, db: DB, user: User, source_id: int, enabled: Annotated[str, Form()]):
    source = db.get(Source, source_id)
    if source is not None:
        source.enabled = enabled == "1"
        db.commit()
    return RedirectResponse("/agencies", status_code=303)
