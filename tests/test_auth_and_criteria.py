import pytest

from app.domain.criteria import CriteriaInput, price_label, typology_label
from app.sources.robots import RobotsInfo, parse_content_signal
from app.web.auth import hash_password, verify_password


def test_password_roundtrip():
    digest = hash_password("segredo-123")
    assert verify_password("segredo-123", digest)
    assert not verify_password("outra", digest)
    assert not verify_password("segredo-123", "")


def test_password_too_long_rejected():
    with pytest.raises(ValueError):
        hash_password("x" * 73)


def test_criteria_validation_catches_inverted_ranges_and_missing_zones():
    data = CriteriaInput(buyer_id=1, typology_min=3, typology_max=2, price_min=500_000, price_max=400_000, zones=[])
    errors = data.validate()
    assert any("Tipologia" in e for e in errors)
    assert any("Preço" in e for e in errors)
    assert any("zona" in e for e in errors)


def test_criteria_valid_open_typology():
    data = CriteriaInput(buyer_id=1, estate_types=["moradia"], typology_min=2, typology_max=None,
                         price_min=400_000, price_max=650_000, must_have=["exterior"], zones=["lisboa/oeiras"])
    assert data.validate() == []
    assert typology_label(2, None) == "T2 ou mais"
    assert price_label(400_000, 650_000) == "400.000 € a 650.000 €"


def test_content_signal_parsing_and_fail_closed_robots():
    text = "User-agent: *\nDisallow: /api\nContent-Signal: search=yes, ai-input=no, ai-train=no\n"
    assert parse_content_signal(text) == {"search": "yes", "ai-input": "no", "ai-train": "no"}
    ok = RobotsInfo(status_code=200, text=text)
    assert ok.allows("https://example.com/pt/1234", "ImoveisBot")
    assert not ok.allows("https://example.com/api/x", "ImoveisBot")
    assert RobotsInfo(status_code=404).allows("https://example.com/anything")
    assert not RobotsInfo(status_code=403).allows("https://example.com/anything")
    assert not RobotsInfo(status_code=None, error="timeout").allows("https://example.com/")
