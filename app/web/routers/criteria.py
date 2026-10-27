from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Buyer, Geo
from app.domain.criteria import CriteriaInput
from app.services import criteria as svc
from app.web.deps import DB, User, render

router = APIRouter(prefix="/criteria")


def _int(value: str | None) -> int | None:
    value = (value or "").replace(".", "").replace(" ", "").strip()
    return int(value) if value else None


def _zone_groups(db: Session) -> list[tuple[Geo, list[Geo]]]:
    rows = list(db.scalars(select(Geo).where(Geo.level.in_(["district", "council"])).order_by(Geo.code)))
    districts = [g for g in rows if g.level == "district"]
    return [(d, [g for g in rows if g.parent_code == d.code]) for d in districts]


def _form_context(db: Session, data: CriteriaInput | None, criteria_id: int | None, errors: list[str]) -> dict:
    return {
        "buyers": list(db.scalars(select(Buyer).order_by(Buyer.label))),
        "zone_groups": _zone_groups(db),
        "data": data or CriteriaInput(buyer_id=0),
        "criteria_id": criteria_id,
        "errors": errors,
    }


def _read_form(
    buyer_id: int,
    transaction: str,
    estate_types: list[str],
    typology_min: str | None,
    typology_max: str | None,
    price_min: str | None,
    price_max: str | None,
    area_min: str | None,
    must_have: list[str],
    nice_to_have: list[str],
    exclude: str | None,
    zones: list[str],
    include_private_owner: str | None,
    include_new_developments: str | None,
    unknown_policy: str,
    source_text: str | None,
) -> CriteriaInput:
    return CriteriaInput(
        buyer_id=buyer_id,
        transaction=transaction,
        estate_types=estate_types,
        typology_min=_int(typology_min),
        typology_max=_int(typology_max),
        price_min=_int(price_min),
        price_max=_int(price_max),
        area_min=_int(area_min),
        must_have=must_have,
        nice_to_have=nice_to_have,
        exclude=[e for e in (exclude or "").split(",")],
        zones=zones,
        include_private_owner=include_private_owner is not None,
        include_new_developments=include_new_developments is not None,
        unknown_policy=unknown_policy,
        source_text=source_text,
        input_channel="form",
    )


FormStr = Annotated[str | None, Form()]
FormList = Annotated[list[str], Form()]


@router.get("/new")
async def new_form(request: Request, db: DB, user: User, buyer: int | None = None):
    data = CriteriaInput(buyer_id=buyer or 0)
    return render(request, "criteria_form.html", **_form_context(db, data, None, []))


@router.post("/new")
async def create(
    request: Request, db: DB, user: User,
    buyer_id: Annotated[int, Form()], transaction: Annotated[str, Form()] = "sale",
    estate_types: FormList = [], typology_min: FormStr = None, typology_max: FormStr = None,
    price_min: FormStr = None, price_max: FormStr = None, area_min: FormStr = None,
    must_have: FormList = [], nice_to_have: FormList = [], exclude: FormStr = None, zones: FormList = [],
    include_private_owner: FormStr = None, include_new_developments: FormStr = None,
    unknown_policy: Annotated[str, Form()] = "show", source_text: FormStr = None,
):
    data = _read_form(buyer_id, transaction, estate_types, typology_min, typology_max, price_min, price_max, area_min,
                      must_have, nice_to_have, exclude, zones, include_private_owner, include_new_developments, unknown_policy, source_text)
    try:
        criteria = svc.create(db, data)
        db.commit()
    except svc.CriteriaError as exc:
        return render(request, "criteria_form.html", **_form_context(db, data, None, exc.errors))
    return RedirectResponse(f"/buyers/{criteria.buyer_id}?msg={quote('Pesquisa criada.')}", status_code=303)


@router.get("/{criteria_id}/edit")
async def edit_form(request: Request, db: DB, user: User, criteria_id: int):
    criteria = svc.get(db, criteria_id)
    if criteria is None:
        return RedirectResponse(f"/buyers?msg={quote('Pesquisa não encontrada.')}", status_code=303)
    return render(request, "criteria_form.html", **_form_context(db, svc.to_input(criteria), criteria_id, []))


@router.post("/{criteria_id}/edit")
async def edit(
    request: Request, db: DB, user: User, criteria_id: int,
    buyer_id: Annotated[int, Form()], transaction: Annotated[str, Form()] = "sale",
    estate_types: FormList = [], typology_min: FormStr = None, typology_max: FormStr = None,
    price_min: FormStr = None, price_max: FormStr = None, area_min: FormStr = None,
    must_have: FormList = [], nice_to_have: FormList = [], exclude: FormStr = None, zones: FormList = [],
    include_private_owner: FormStr = None, include_new_developments: FormStr = None,
    unknown_policy: Annotated[str, Form()] = "show", source_text: FormStr = None,
):
    data = _read_form(buyer_id, transaction, estate_types, typology_min, typology_max, price_min, price_max, area_min,
                      must_have, nice_to_have, exclude, zones, include_private_owner, include_new_developments, unknown_policy, source_text)
    try:
        criteria = svc.update(db, criteria_id, data)
        db.commit()
    except svc.CriteriaError as exc:
        return render(request, "criteria_form.html", **_form_context(db, data, criteria_id, exc.errors))
    return RedirectResponse(f"/buyers/{criteria.buyer_id}?msg={quote('Pesquisa atualizada.')}", status_code=303)


@router.post("/{criteria_id}/toggle")
async def toggle(request: Request, db: DB, user: User, criteria_id: int, active: Annotated[str, Form()]):
    criteria = svc.set_active(db, criteria_id, active == "1")
    db.commit()
    return RedirectResponse(f"/buyers/{criteria.buyer_id}", status_code=303)
