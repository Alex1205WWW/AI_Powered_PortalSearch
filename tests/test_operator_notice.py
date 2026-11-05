"""A message from Alex, shown in the panel.

She still has no Telegram connected, so the panel is the only place anything from him can reach her.
It is deliberately NOT the health banner: a warning style means "something is broken", and mixing a note
in with faults teaches her to skim past both.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import appsettings

BASE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "base.html"
CSS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "app.css"


@pytest.fixture
def store(monkeypatch):
    data = {}
    monkeypatch.setattr(appsettings, "get", lambda _s, k, d=None: data.get(k, d))
    monkeypatch.setattr(appsettings, "put", lambda _s, k, v: data.__setitem__(k, v))
    return data


def test_no_notice_by_default(store):
    assert appsettings.notice(None) is None


def test_saving_and_reading_back(store):
    assert appsettings.set_notice(None, "Olá Andreia, já pode ligar o Telegram.") == (True, False)
    saved = appsettings.notice(None)
    assert saved["text"] == "Olá Andreia, já pode ligar o Telegram."
    assert saved["updated_at"]


def test_blank_clears_it(store):
    appsettings.set_notice(None, "alguma coisa")
    assert appsettings.set_notice(None, "   ") == (False, False)
    assert appsettings.notice(None) is None


def test_whitespace_only_never_becomes_an_empty_banner(store):
    """An empty bordered box at the top of every page looks like a bug."""
    appsettings.set_notice(None, "\n\n   \t  \n")
    assert appsettings.notice(None) is None


def test_the_text_is_capped_and_says_so(store):
    """It sits above every page, so there is a cap - but cutting her last sentence off without a word is
    how a notice ends up stopping mid-sentence with nobody knowing why."""
    stored, truncated = appsettings.set_notice(None, "A" * 5000)
    assert (stored, truncated) == (True, True)
    assert len(appsettings.notice(None)["text"]) == appsettings.NOTICE_MAX_CHARS

    stored, truncated = appsettings.set_notice(None, "A" * 10)
    assert (stored, truncated) == (True, False), "a short note must not claim to be cut"


def test_the_save_message_reports_a_cut():
    from pathlib import Path

    router = (Path(__file__).resolve().parents[1] / "app" / "web" / "routers" / "settings_page.py").read_text(encoding="utf-8")
    assert "cortado a" in router


def test_editing_changes_the_timestamp(store):
    appsettings.set_notice(None, "primeira versão")
    first = appsettings.notice(None)["updated_at"]
    store[appsettings.OPERATOR_NOTICE] = {"text": "primeira versão", "updated_at": "2020-01-01T00:00:00+00:00"}
    appsettings.set_notice(None, "segunda versão")
    assert appsettings.notice(None)["updated_at"] != "2020-01-01T00:00:00+00:00"
    assert appsettings.notice(None)["updated_at"] >= first[:4]


def test_a_dismissed_notice_reappears_when_edited():
    """The dismissal is remembered against the notice's own timestamp, so a NEW message is never hidden
    by an old dismissal - which would make the feature silently useless."""
    html = BASE.read_text(encoding="utf-8")
    assert 'data-stamp="{{ notice.updated_at }}"' in html
    assert 'dismissed !== stamp' in html


def test_it_is_hidden_until_the_script_decides():
    """Rendered with `hidden`, revealed by the script. The other way round flashes a notice she already
    dismissed on every single page load."""
    html = BASE.read_text(encoding="utf-8")
    assert 'id="op-notice"' in html and "hidden>" in html
    assert "box.hidden = false" in html


def test_it_sits_above_the_health_alerts_and_looks_different():
    html = BASE.read_text(encoding="utf-8")
    assert html.index('id="op-notice"') < html.index("source-down"), "a note must not be buried under faults"
    css = CSS.read_text(encoding="utf-8")
    assert "article.notice" in css and "--accent" in css.split("article.notice")[1][:200]
    assert "--warn" not in css.split("article.notice")[1][:200], "a note must not use the fault colour"


def test_her_line_breaks_survive():
    """A multi-paragraph message collapsing into one block would be unreadable."""
    assert "white-space: pre-line" in CSS.read_text(encoding="utf-8")


def test_settings_can_write_and_clear_it():
    router = (Path(__file__).resolve().parents[1] / "app" / "web" / "routers" / "settings_page.py").read_text(encoding="utf-8")
    assert "set_notice" in router and "notice_text" in router
    tpl = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
    assert 'name="notice_text"' in tpl


def test_a_broken_notice_never_takes_a_page_down():
    import inspect

    from app.web import deps

    source = inspect.getsource(deps._notice)
    assert "except Exception" in source and "return None" in source


def test_resaving_the_same_words_keeps_the_stamp(store):
    """The settings form posts every field together. Changing her alert hours re-submits the unchanged
    notice, and bumping the stamp there would resurrect a notice she had already dismissed - every time
    she touched an unrelated setting."""
    appsettings.set_notice(None, "Olá Andreia.")
    first = appsettings.notice(None)["updated_at"]

    appsettings.set_notice(None, "Olá Andreia.")          # same words, e.g. a digest-hour change
    assert appsettings.notice(None)["updated_at"] == first

    appsettings.set_notice(None, "  Olá Andreia.  ")      # same words, different whitespace
    assert appsettings.notice(None)["updated_at"] == first


def test_changing_the_words_does_bump_the_stamp(store):
    """The other half: an edited message must reach her even if she dismissed the previous one."""
    appsettings.set_notice(None, "primeira versão")
    first = appsettings.notice(None)["updated_at"]
    store[appsettings.OPERATOR_NOTICE]["updated_at"] = "2020-01-01T00:00:00+00:00"

    appsettings.set_notice(None, "segunda versão")
    assert appsettings.notice(None)["updated_at"] != "2020-01-01T00:00:00+00:00"
    assert appsettings.notice(None)["text"] == "segunda versão"


def test_clearing_then_writing_the_same_text_is_a_new_message(store):
    """She dismissed it, Alex removed it, then put the same words back - that is a deliberate re-send."""
    appsettings.set_notice(None, "mesmo texto")
    store[appsettings.OPERATOR_NOTICE]["updated_at"] = "2020-01-01T00:00:00+00:00"
    appsettings.set_notice(None, "")
    appsettings.set_notice(None, "mesmo texto")
    assert appsettings.notice(None)["updated_at"] != "2020-01-01T00:00:00+00:00"


# --- the reply link, and a notice she cannot switch off for good --------------------------------

def test_the_link_is_stored_and_offered(store):
    appsettings.set_notice(None, "texto", "https://www.workana.com/inbox/x/alex-wu-3")
    assert appsettings.notice(None)["url"] == "https://www.workana.com/inbox/x/alex-wu-3"


def test_only_http_links_are_accepted(store):
    """The URL is rendered into an href. A `javascript:` scheme there would be a hole, and a bad link is
    worse than no link."""
    for bad in ("javascript:alert(1)", "data:text/html,x", "workana.com/inbox", " "):
        appsettings.set_notice(None, "texto", bad)
        assert appsettings.notice(None)["url"] == "", bad
    appsettings.set_notice(None, "texto", "http://example.com")
    assert appsettings.notice(None)["url"] == "http://example.com"


def test_changing_only_the_link_brings_the_notice_back(store):
    """A new destination is a new message as far as she is concerned."""
    appsettings.set_notice(None, "texto", "https://a.example")
    first = appsettings.notice(None)["updated_at"]
    store[appsettings.OPERATOR_NOTICE]["updated_at"] = "2020-01-01T00:00:00+00:00"
    appsettings.set_notice(None, "texto", "https://b.example")
    assert appsettings.notice(None)["updated_at"] != "2020-01-01T00:00:00+00:00"


def test_closing_it_lasts_only_the_visit():
    """Alex asked that she cannot switch it off permanently: closing quiets it for this visit, and it is
    back on the next one. Only removing the text in Configurações ends it."""
    html = BASE.read_text(encoding="utf-8")
    assert "sessionStorage.getItem" in html and "sessionStorage.setItem" in html
    assert "localStorage.getItem(key)" not in html, "localStorage would switch it off for good"


def test_the_reply_button_opens_the_conversation_safely():
    html = BASE.read_text(encoding="utf-8")
    assert 'class="notice-reply"' in html
    assert 'target="_blank"' in html and 'rel="noopener noreferrer"' in html, "a new tab must not keep opener access"
    assert "Responder ao Alex" in html


def test_opening_the_reply_also_steps_the_banner_aside():
    html = BASE.read_text(encoding="utf-8")
    assert "reply.addEventListener" in html and html.count('sessionStorage.setItem(key, stamp)') == 2


def test_settings_can_set_the_link():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    router = (root / "app" / "web" / "routers" / "settings_page.py").read_text(encoding="utf-8")
    tpl = (root / "app" / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
    assert "notice_url" in router and 'name="notice_url"' in tpl
