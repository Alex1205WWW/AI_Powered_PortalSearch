from dataclasses import dataclass, field

from app.domain.listing import CanonicalListing, features_from_text, merge_features
from app.domain.matching import GRADE_CONFIRMED, GRADE_POSSIBLE, evaluate, tier_label


@dataclass
class C:  # minimal criteria stand-in
    transaction: str = "sale"
    estate_types: list = field(default_factory=lambda: ["moradia"])
    typology_min: int | None = 2
    typology_max: int | None = None
    price_min: int | None = 400_000
    price_max: int | None = 650_000
    area_min: int | None = None
    must_have: list = field(default_factory=lambda: ["exterior"])
    nice_to_have: list = field(default_factory=lambda: ["garagem"])
    exclude: list = field(default_factory=list)
    include_private_owner: bool = True
    include_new_developments: bool = True
    unknown_policy: str = "show"


ROSA_ZONES = ["lisboa/lisboa", "lisboa/oeiras", "lisboa/sintra", "lisboa/loures"]


def listing(**kw) -> CanonicalListing:
    base = dict(source_key="imovirtual", external_id="1", url="https://x/1", price=520_000, rooms=3,
                estate_type="moradia", geo_code="lisboa/sintra/algueirao", area_util=140.0)
    base.update(kw)
    return CanonicalListing(**base)


def test_confirmed_when_everything_known_and_passing():
    l = listing(features=merge_features({"garden": None}, {}) | {"exterior": {"value": True, "tier": "confirmed"}})
    r = evaluate(l, C(), ROSA_ZONES)
    assert r.ok and r.grade == GRADE_CONFIRMED
    assert "zona" in r.satisfied and "exterior" in r.satisfied and r.unknown == []


def test_unknown_must_have_is_possivel_never_confirmada():
    r = evaluate(listing(features={}), C(), ROSA_ZONES)
    assert r.ok and r.grade == GRADE_POSSIBLE
    assert r.unknown == ["exterior"]
    assert tier_label({}, "exterior") == "não indicado"


def test_hide_policy_rejects_unknown_must_have():
    r = evaluate(listing(features={}), C(unknown_policy="hide"), ROSA_ZONES)
    assert not r.ok and "exterior:nao-indicado" in r.unsatisfied


def test_absent_typed_feature_rejects():
    r = evaluate(listing(features=merge_features({"exterior": False}, {})), C(), ROSA_ZONES)
    assert not r.ok and "exterior" in r.unsatisfied


def test_open_upper_typology_bound_accepts_t6():
    r = evaluate(listing(rooms=6, features={"exterior": {"value": True, "tier": "mentioned"}}), C(), ROSA_ZONES)
    assert r.ok and "tipologia" in r.satisfied


def test_hard_filters_reject():
    assert "zona" in evaluate(listing(geo_code="setubal/almada"), C(), ROSA_ZONES).unsatisfied
    assert "zona" in evaluate(listing(geo_code=None), C(), ROSA_ZONES).unsatisfied
    assert "preco" in evaluate(listing(price=700_000), C(), ROSA_ZONES).unsatisfied
    assert "tipologia" in evaluate(listing(rooms=1), C(), ROSA_ZONES).unsatisfied
    assert "tipo" in evaluate(listing(estate_type="apartamento"), C(), ROSA_ZONES).unsatisfied
    assert "ativo" in evaluate(listing(status="sold"), C(), ROSA_ZONES).unsatisfied


def test_hidden_price_and_missing_area_are_unknown_not_fail():
    r = evaluate(listing(hide_price=True, area_util=None, area_bruta=None, features={"exterior": {"value": True, "tier": "confirmed"}}),
                 C(area_min=100), ROSA_ZONES)
    assert r.ok and r.grade == GRADE_POSSIBLE and set(r.unknown) == {"preco", "area"}


def test_district_zone_covers_all_councils():
    r = evaluate(listing(geo_code="lisboa/mafra", features={"exterior": {"value": True, "tier": "confirmed"}}), C(), ["lisboa"])
    assert r.ok and "zona" in r.satisfied


def test_exclude_terms_and_private_owner_flag():
    r = evaluate(listing(title="Moradia T3 para recuperar com terreno"), C(exclude=["para recuperar"]), ROSA_ZONES)
    assert not r.ok and any(u.startswith("excluir:") for u in r.unsatisfied)
    r = evaluate(listing(is_private_owner=True, features={"exterior": {"value": True, "tier": "confirmed"}}), C(include_private_owner=False), ROSA_ZONES)
    assert not r.ok and "particular" in r.unsatisfied


def test_features_from_text_is_accent_insensitive_regex():
    hits = features_from_text("Moradia T3 com jardim e garagem, terraço com vista de rio, sem elevador")
    assert {"jardim", "garagem", "exterior", "terraco", "vista", "elevador"} <= set(hits)
    assert all(h["tier"] == "mentioned" for h in hits.values())
    merged = merge_features({"elevator": None, "garagem": False}, hits)
    assert merged["garagem"] == {"value": False, "tier": "absent"}  # typed 'no' beats a keyword hit
    assert merged["jardim"]["tier"] == "mentioned"
