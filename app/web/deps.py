from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from pydantic import BeforeValidator
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
    context.setdefault("path", request.url.path)   # so the nav can mark where she is
    context.setdefault("health_banner", _banner())
    return templates.TemplateResponse(request, name, context)


def _banner() -> list[dict]:
    """Broken sources, shown on every page (deliverable 15). It opens its own short session so no router has
    to remember to pass one, and a banner that cannot be built never takes the page down with it."""
    from app.db.session import session_scope
    from app.services import health

    try:
        with session_scope() as session:
            return health.banner(session)
    except Exception:  # noqa: BLE001
        return []


# --- query parameters that come from HTML forms -------------------------------------------------
# A browser submits EVERY field in a form, including the ones left blank, so `<option value="">Todos os
# compradores</option>` arrives as `buyer=`. Pydantic cannot parse "" into `int | None` - it is not the same
# as the parameter being absent - and FastAPI answers 422 with a raw JSON error page.
#
# This is why pressing "Filtrar" with nothing selected returned
#   {"detail":[{"type":"int_parsing","loc":["query","buyer"], ...}]}
# It had been true of `buyer` since the page was written; it only became obvious when more numeric filters
# were added. Test optional query parameters by submitting the whole form, not by hand-crafting a URL with
# only the parameters you care about.

def _blank_to_none(value):
    return None if isinstance(value, str) and not value.strip() else value


OptInt = Annotated[int | None, BeforeValidator(_blank_to_none)]
OptStr = Annotated[str | None, BeforeValidator(_blank_to_none)]
