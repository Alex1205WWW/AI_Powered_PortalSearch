from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.db.models import Buyer, Criteria, Listing, Property, PropertyMatch, Source
from app.services import digest
from app.web import auth
from app.web.deps import DB, User, render

router = APIRouter()


@router.get("/login")
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None)


@router.post("/login")
def login_submit(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    client_ip = request.client.host if request.client else "unknown"
    if auth.rate_limited(client_ip):
        return render(request, "login.html", error="Demasiadas tentativas. Aguarde um minuto.")
    if not auth.check_credentials(username.strip(), password):
        return render(request, "login.html", error="Utilizador ou password incorretos.")
    auth.login(request, username.strip())
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
def logout(request: Request):
    auth.logout(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/")
def dashboard(request: Request, db: DB, user: User):
    counts = {
        "buyers_active": db.scalar(select(func.count()).select_from(Buyer).where(Buyer.active.is_(True))) or 0,
        "buyers_paused": db.scalar(select(func.count()).select_from(Buyer).where(Buyer.active.is_(False))) or 0,
        "criteria_active": db.scalar(select(func.count()).select_from(Criteria).where(Criteria.active.is_(True))) or 0,
        "listings": db.scalar(select(func.count()).select_from(Listing)) or 0,
        "matches_new": db.scalar(select(func.count()).select_from(PropertyMatch).where(PropertyMatch.state == "novo")) or 0,
    }
    counts["properties"] = db.scalar(select(func.count()).select_from(Property)) or 0
    counts["matches_confirmed"] = db.scalar(
        select(func.count()).select_from(PropertyMatch).where(PropertyMatch.grade == "confirmada")) or 0
    # Reuses the digest's own summary rather than a second, slightly different definition of "new": it
    # counts the alerts actually queued for her, so the dashboard and her morning message agree.
    buyer_rows = digest.per_buyer(db, datetime.now(timezone.utc) - timedelta(hours=24))
    sources = list(db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.name)))
    # The channel and worker rows are health, not sources she chose; the banner already reports them.
    crawl_sources = [s for s in sources if s.kind not in ("channel", "worker")]
    return render(request, "dashboard.html", counts=counts, sources=crawl_sources, buyer_rows=buyer_rows)
