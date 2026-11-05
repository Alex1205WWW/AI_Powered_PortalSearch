"""She says the administrative level out loud, and in her three districts it is the only thing that
settles the name.

`lisboa` is a district AND `lisboa/lisboa` is a council; so are Setúbal and Santarém. A bare "Lisboa" is
therefore permanently ambiguous and costs her a round-trip every time. "concelho de Lisboa" is not noise
to be stripped - it is the answer, and before this it resolved to nothing at all, which was worse than
the ambiguity.
"""
from __future__ import annotations

import pytest

from app.geo import resolve

COUNCIL_SPELLINGS = ["concelho de Lisboa", "Concelho de Lisboa", "conselho de Lisboa",
                     "no concelho de Lisboa", "concelho Lisboa", "município de Lisboa"]


@pytest.mark.parametrize("text", COUNCIL_SPELLINGS)
def test_every_spelling_she_might_use_names_the_council(text):
    level, bare = resolve.split_admin_level(text)
    assert level == "council"
    assert bare == "Lisboa"


def test_whisper_spells_concelho_as_conselho_and_both_must_work():
    # Homophones in Portuguese. Dropping "conselho" would break the voice path while the typed path worked.
    assert resolve.split_admin_level("conselho de Oeiras") == ("council", "Oeiras")
    assert resolve.split_admin_level("concelho de Oeiras") == ("council", "Oeiras")


def test_district_and_parish_qualifiers():
    assert resolve.split_admin_level("distrito de Setúbal") == ("district", "Setúbal")
    assert resolve.split_admin_level("freguesia de Alcântara") == ("parish", "Alcântara")


def test_a_plain_place_name_is_untouched():
    assert resolve.split_admin_level("Lisboa") == (None, "Lisboa")
    assert resolve.split_admin_level("Vila Franca de Xira") == (None, "Vila Franca de Xira")


def test_the_qualifier_alone_is_not_a_place():
    # "concelho" with nothing after it must not become an empty search that matches everything.
    assert resolve.split_admin_level("concelho") == (None, "concelho")
    assert resolve.split_admin_level("") == (None, "")
    assert resolve.split_admin_level("   ") == (None, "")


def test_the_remainder_keeps_her_spelling_not_a_slug():
    # The caller quotes it back when it has to ask; a slug there would read as a machine error.
    assert resolve.split_admin_level("concelho de Vila Franca de Xira") == ("council", "Vila Franca de Xira")


class _Geo:
    def __init__(self, name, level):
        self.name, self.level = name, level


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def get(self, _model, code):
        return self._rows.get(code)


def test_the_confirmation_card_says_which_lisboa_it_took():
    # The card exists for her to catch a mistake. "Lisboa" alone is uncheckable - district and council
    # share the name, as do Setúbal and Santarém, which is her entire working area.
    from app.bot.handlers.intake import _zone_names
    session = _Session({"lisboa": _Geo("Lisboa", "district"),
                        "lisboa/lisboa": _Geo("Lisboa", "council"),
                        "lisboa/lisboa/alcantara": _Geo("Alcântara", "parish")})
    assert _zone_names(session, {"zone_codes": ["lisboa/lisboa"]}) == ["Lisboa (concelho)"]
    assert _zone_names(session, {"zone_codes": ["lisboa"]}) == ["Lisboa (distrito)"]
    assert _zone_names(session, {"zone_codes": ["lisboa/lisboa/alcantara"]}) == ["Alcântara (freguesia)"]


def test_an_unknown_code_still_renders_rather_than_crashing_the_card():
    from app.bot.handlers.intake import _zone_names
    assert _zone_names(_Session({}), {"zone_codes": ["nao/existe"]}) == ["nao/existe"]
    assert _zone_names(_Session({}), {}) == []
