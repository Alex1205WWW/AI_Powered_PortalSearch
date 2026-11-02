"""Nothing that talks to Postgres may run on an event loop.

Every session in this project is a synchronous SQLAlchemy session over sync psycopg. An `async def` that
calls one does not yield while it waits, so the whole loop stops until the query returns: in the panel that
stalls every other request including `/healthz`, and in the bot it stops answering her at all.

The project already knew this - `app/bot/common.py` says so in its own module docstring and routes handler
work through `in_db` - and both halves had drifted away from it anyway. The panel had 28 handlers declared
`async` with no `await` anywhere in them, and the bot called the database synchronously from `guard`, on
the path every single update takes. Both are cheap mistakes to make and invisible until the system is busy,
so they are asserted here rather than left to review.

The rule:
  * a panel route is a plain `def`, so Starlette runs it in a thread (bcrypt at login belongs there too);
  * bot code reaches the database only through `in_db` or another coroutine that goes to a thread.
"""
from __future__ import annotations

import inspect

import pytest

from app.web import routers


def _panel_endpoints():
    """Every endpoint on every `APIRouter` under `app.web.routers`.

    Read off the routers themselves rather than off `app.routes`: FastAPI keeps an included router nested
    behind a private wrapper there, so walking the app finds only `/healthz` and every assertion below
    would pass on an empty list. `APIRouter.routes` is public and does not move between versions.
    """
    import importlib
    import pkgutil

    seen: list[tuple[str, object]] = []
    for info in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"{routers.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if router is None:
            continue
        for route in router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                seen.append((f"{info.name}.{endpoint.__name__}", endpoint))
    return seen


def test_there_are_panel_routes_to_check():
    """A silent empty list would make every assertion below vacuously true."""
    assert len(_panel_endpoints()) >= 25


@pytest.mark.parametrize("name,endpoint", _panel_endpoints(), ids=[n for n, _ in _panel_endpoints()])
def test_a_panel_route_is_sync_so_it_runs_in_a_thread(name, endpoint):
    assert not inspect.iscoroutinefunction(endpoint), (
        f"{name} is `async def` but the database session it uses is synchronous. Drop the `async`: "
        "Starlette then runs it in a worker thread and the blocking call stops holding up the loop."
    )


def test_the_bots_allowlist_check_goes_to_a_thread():
    """`allowed` runs on every update and reads the stored chat id from Postgres."""
    from app.bot import common

    assert inspect.iscoroutinefunction(common.allowed)
    assert inspect.iscoroutinefunction(common.adopt_chat_id)


def test_no_bot_handler_opens_a_session_itself():
    """Handlers go through `in_db` (or an awaited helper); a bare `session_scope()` in handler code is the
    blocking pattern this file exists to prevent."""
    import pathlib

    handlers = pathlib.Path(common_dir := "app/bot/handlers").glob("*.py")
    offenders = [p.name for p in handlers if "session_scope" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"{offenders} open a session directly; use `in_db` ({common_dir})"
