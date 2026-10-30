"""Every panel form must survive being submitted with nothing filled in.

A browser submits **all** fields of a form, including the blank ones, so `<option value="">Todos os
compradores</option>` arrives at the server as `buyer=`. Pydantic cannot read "" as `int | None` - that is
not the same as the parameter being absent - and FastAPI answers 422 with a raw JSON error page.

That is what pressing "Filtrar" showed the client:
    {"detail":[{"type":"int_parsing","loc":["query","buyer"],...}]}

It had been true of `buyer` since the page was first written and was missed because the filters were tested
by hand-crafting URLs containing only the interesting parameters. These tests submit the whole form the way
a browser does, which is the only version that matters.
"""
from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture(scope="module")
def client():
    settings.admin_password_hash = bcrypt.hashpw(b"test-pw", bcrypt.gensalt()).decode()
    from app.web.main import app

    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": settings.admin_username, "password": "test-pw"})
    return c


BLANK_GETS = [
    ("imóveis", "/properties?buyer=&grade=&state=&zone=&estate=&price_min=&price_max=&rooms_min=&rooms_max=&q="),
    ("mediadoras", "/agencies?f=&q=&page="),
    ("nova pesquisa", "/criteria/new?buyer="),
    ("exportação", "/properties/export.xlsx?buyer=&state=&grade="),
]

BLANK_POSTS = [
    ("configurações", "/settings", {"alert_from": "", "alert_to": "", "digest_at": "", "cap_usd": ""}),
    ("adicionar mediadora", "/agencies/add", {"website_url": "", "name": ""}),
    ("estado", "/matches/1/state", {"state": "novo", "commission_share_pct": "", "back": "/properties"}),
]


@pytest.mark.parametrize("name,url", BLANK_GETS, ids=[n for n, _ in BLANK_GETS])
def test_a_blank_filter_submission_is_not_an_error_page(client, name, url):
    assert client.get(url).status_code < 400


@pytest.mark.parametrize("name,url,data", BLANK_POSTS, ids=[n for n, _, _ in BLANK_POSTS])
def test_a_blank_form_post_is_not_an_error_page(client, name, url, data):
    assert client.post(url, data=data).status_code < 400


def test_partially_filled_filters_still_filter(client):
    """The blank-tolerant types must not swallow the values that ARE set."""
    response = client.get("/properties?buyer=&grade=confirmada&price_max=600000&rooms_min=2&zone=&q=")
    assert response.status_code == 200
    assert "confirmada" in response.text


def test_a_genuinely_invalid_value_gets_a_sentence_not_json(client):
    """The safety net: anything still invalid must come back as readable Portuguese, never raw JSON."""
    response = client.get("/properties?price_min=abc")
    assert response.status_code == 303
    assert "Valor%20inv" in response.headers.get("location", "")
    assert "int_parsing" not in response.text


def test_blank_and_absent_mean_the_same_thing(client):
    """`?buyer=` and no `buyer` at all must produce the same page."""
    import re

    def shown(url):
        html = client.get(url).text
        return re.search(r'<h2>Imóveis <small class="muted">(\d+)', html).group(1)

    assert shown("/properties") == shown("/properties?buyer=&state=&grade=")
