from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.db.models import Buyer, Criteria, Listing, PropertyMatch, Source
from app.web import auth
from app.web.deps import DB, User, render

router = APIRouter()


@router.get("/login")
async def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None)


@router.post("/login")
async def login_submit(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    client_ip = request.client.host if request.client else "unknown"
    if auth.rate_limited(client_ip):
        return render(request, "login.html", error="Demasiadas tentativas. Aguarde um minuto.")
    if not auth.check_credentials(username.strip(), password):
        return render(request, "login.html", error="Utilizador ou password incorretos.")
    auth.login(request, username.strip())
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    auth.logout(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/")
async def dashboard(request: Request, db: DB, user: User):
    counts = {
        "buyers_active": db.scalar(select(func.count()).select_from(Buyer).where(Buyer.active.is_(True))) or 0,
        "buyers_paused": db.scalar(select(func.count()).select_from(Buyer).where(Buyer.active.is_(False))) or 0,
        "criteria_active": db.scalar(select(func.count()).select_from(Criteria).where(Criteria.active.is_(True))) or 0,
        "listings": db.scalar(select(func.count()).select_from(Listing)) or 0,
        "matches_new": db.scalar(select(func.count()).select_from(PropertyMatch).where(PropertyMatch.state == "novo")) or 0,
    }
    sources = list(db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.name)))
    return render(request, "dashboard.html", counts=counts, sources=sources)
