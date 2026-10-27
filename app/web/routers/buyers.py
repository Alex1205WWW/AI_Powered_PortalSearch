from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.services import buyers as svc
from app.web.deps import DB, User, render

router = APIRouter(prefix="/buyers")


@router.get("")
async def list_buyers(request: Request, db: DB, user: User):
    return render(request, "buyers_list.html", buyers=svc.list_all(db), error=None)


@router.post("")
async def create_buyer(request: Request, db: DB, user: User, label: Annotated[str, Form()]):
    try:
        buyer = svc.create(db, label)
        db.commit()
    except svc.BuyerError as exc:
        return render(request, "buyers_list.html", buyers=svc.list_all(db), error=str(exc))
    return RedirectResponse(f"/buyers/{buyer.id}?msg={quote('Comprador criado.')}", status_code=303)


@router.get("/{buyer_id}")
async def buyer_detail(request: Request, db: DB, user: User, buyer_id: int):
    buyer = svc.get(db, buyer_id)
    if buyer is None:
        return RedirectResponse(f"/buyers?msg={quote('Comprador não encontrado.')}", status_code=303)
    return render(request, "buyer_detail.html", buyer=buyer)


@router.post("/{buyer_id}/pause")
async def pause_buyer(request: Request, db: DB, user: User, buyer_id: int):
    svc.pause(db, buyer_id)
    db.commit()
    return RedirectResponse(f"/buyers/{buyer_id}?msg={quote('Em pausa: continua a ser vigiado, sem alertas.')}", status_code=303)


@router.post("/{buyer_id}/resume")
async def resume_buyer(request: Request, db: DB, user: User, buyer_id: int):
    svc.resume(db, buyer_id)
    db.commit()
    return RedirectResponse(f"/buyers/{buyer_id}?msg={quote('Retomado.')}", status_code=303)
