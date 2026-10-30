"""Panel entrypoint: FastAPI + Jinja2 + HTMX, behind Caddy TLS. Run: uvicorn app.web.main:app"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.logging_setup import setup_logging
from app.web.auth import NotAuthenticated
from app.web.routers import agencies, buyers, criteria, dashboard, findings, properties, settings_page, sources

setup_logging(settings.log_level)
log = logging.getLogger(__name__)

app = FastAPI(title="Imóveis — painel", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.app_env == "prod",
    max_age=60 * 60 * 24 * 14,
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


@app.exception_handler(NotAuthenticated)
async def _redirect_to_login(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url=f"/login?next={quote(str(request.url.path))}", status_code=303)


@app.exception_handler(OperationalError)
async def _database_unavailable(request: Request, exc: OperationalError):
    log.error("database unavailable: %s", str(exc).splitlines()[0])
    body = ("<h2>Base de dados indisponível</h2><p>O painel não consegue ligar ao Postgres. "
            "Confirme que a base de dados está a correr (em desenvolvimento: <code>scripts/dev.ps1</code>; em produção: "
            "<code>docker compose ps</code>) e volte a tentar.</p>")
    return HTMLResponse(body, status_code=503)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}


app.include_router(dashboard.router)
app.include_router(buyers.router)
app.include_router(criteria.router)
app.include_router(properties.router)
app.include_router(sources.router)
app.include_router(agencies.router)
app.include_router(settings_page.router)
app.include_router(findings.router)
