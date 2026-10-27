from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.services import sources as svc
from app.web.deps import DB, User, render

router = APIRouter(prefix="/sources")


@router.get("")
async def list_sources(request: Request, db: DB, user: User):
    return render(request, "sources.html", sources=svc.list_all(db))


@router.post("/{source_id}/toggle")
async def toggle(request: Request, db: DB, user: User, source_id: int, enabled: Annotated[str, Form()]):
    svc.set_enabled(db, source_id, enabled == "1")
    db.commit()
    return RedirectResponse("/sources", status_code=303)


@router.post("/refresh-robots")
async def refresh_robots(request: Request, db: DB, user: User):
    results = svc.refresh_robots(db)
    db.commit()
    return RedirectResponse(f"/sources?msg={quote(f'robots.txt atualizado em {len(results)} fontes.')}", status_code=303)
