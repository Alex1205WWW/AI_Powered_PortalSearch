import pytest

from app.ai.client import AiGuardError, assert_llm_allowed
from app.domain.events import GRADE_OUT, derive
from app.domain.matching import GRADE_CONFIRMED, GRADE_POSSIBLE, MatchResult


def ok(grade=GRADE_CONFIRMED):
    return MatchResult(ok=True, grade=grade)


def test_initial_runs_never_alert():
    assert derive(result=ok(), created=True, previous_grade=None, listing_is_new=True, price_changed=False,
                  old_price=None, new_price=500000, initial=True) == []


def test_new_listing_alerts_once_as_new():
    assert derive(result=ok(), created=True, previous_grade=None, listing_is_new=True, price_changed=False,
                  old_price=None, new_price=500000, initial=False) == ["new"]


def test_known_listing_dropping_into_range_is_price_drop():
    assert derive(result=ok(), created=True, previous_grade=None, listing_is_new=False, price_changed=True,
                  old_price=700000, new_price=640000, initial=False) == ["price_drop"]
    assert derive(result=ok(), created=False, previous_grade=GRADE_OUT, listing_is_new=False, price_changed=True,
                  old_price=700000, new_price=640000, initial=False) == ["price_drop"]


def test_price_rise_or_no_change_does_not_alert():
    assert derive(result=ok(), created=False, previous_grade=GRADE_CONFIRMED, listing_is_new=False, price_changed=True,
                  old_price=600000, new_price=620000, initial=False) == []
    assert derive(result=ok(), created=False, previous_grade=GRADE_CONFIRMED, listing_is_new=False, price_changed=False,
                  old_price=600000, new_price=600000, initial=False) == []


def test_possivel_to_confirmada_is_data_completed():
    assert derive(result=ok(GRADE_CONFIRMED), created=False, previous_grade=GRADE_POSSIBLE, listing_is_new=False,
                  price_changed=False, old_price=600000, new_price=600000, initial=False) == ["data_completed"]


def test_non_match_never_alerts():
    assert derive(result=MatchResult(ok=False, grade=None), created=False, previous_grade=None, listing_is_new=True,
                  price_changed=False, old_price=None, new_price=1, initial=False) == []


def test_imovirtual_content_can_never_reach_an_llm():
    with pytest.raises(AiGuardError):
        assert_llm_allowed("imovirtual", purpose="profile")
    with pytest.raises(AiGuardError):
        assert_llm_allowed("Imovirtual", purpose="parse")
    assert_llm_allowed("remax", purpose="profile")
    assert_llm_allowed(None, purpose="voice-note")
