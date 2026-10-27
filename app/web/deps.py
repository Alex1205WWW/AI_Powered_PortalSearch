from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain import criteria as vocab
from app.web.auth import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals.update(
    TRANSACTIONS=vocab.TRANSACTIONS,
    ESTATE_TYPES=vocab.ESTATE_TYPES,
    FEATURES=vocab.FEATURES,
    UNKNOWN_POLICIES=vocab.UNKNOWN_POLICIES,
    typology_label=vocab.typology_label,
    price_label=vocab.price_label,
)

DB = Annotated[Session, Depends(get_db)]
User = Annotated[str, Depends(current_user)]


def render(request: Request, name: str, **context):
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("user", request.session.get("user"))
    return templates.TemplateResponse(request, name, context)
