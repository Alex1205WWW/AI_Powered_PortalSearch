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


def test_listing_located_only_at_district_level_is_unknown_for_a_council_zone():
    """An alert email may only say "Lisboa" (district). That is "não indicado" at council granularity, not a rejection."""
    r = evaluate(listing(geo_code="lisboa", features={"exterior": {"value": True, "tier": "confirmed"}}), C(), ROSA_ZONES)
    assert r.ok and r.grade == GRADE_POSSIBLE and "zona" in r.unknown
    r = evaluate(listing(geo_code="setubal", features={"exterior": {"value": True, "tier": "confirmed"}}), C(), ROSA_ZONES)
    assert not r.ok and "zona" in r.unsatisfied
    r = evaluate(listing(geo_code="lisboa", features={"exterior": {"value": True, "tier": "confirmed"}}), C(unknown_policy="hide"), ROSA_ZONES)
    assert r.ok and r.grade == GRADE_POSSIBLE  # hide is about obligatory features; a coarse zone stays 'possivel'
    r = evaluate(listing(geo_code="lisboa", features={}), C(unknown_policy="hide"), ROSA_ZONES)
    assert not r.ok and "exterior:nao-indicado" in r.unsatisfied and "zona:nao-indicado" not in r.unsatisfied


# --- "Reservado"/"Vendido" in the title (2026-11-03, her request) --------------------------------
# She asked: "também estão aparecer opções que já dizem 'Reservado', essas não é necessário que
# apareçam". Every title below is real, taken from production on 2026-11-04, and the rule was validated
# against the WHOLE population of 101 titles containing these words: 92 sold/reserved, 9 kept, zero
# misclassified in either direction.

import pytest  # noqa: E402

from app.domain.listing import sold_from_title  # noqa: E402


@pytest.mark.parametrize("title", [
    "Apartamento T2 Vendido Lisboa",
    "Apartamento T2 Vendido Seixal",
    "Moradia T5+1 Vendido Oeiras",
    "Apartamento T3 Reservado Lisboa",
    "Moradia T4 em Agualva-Cacém (RESERVADO)",
    "RESERVADO: Moradia T3+1 em São João dos Montes",
    "** VENDIDO ** Terreno com Projeto Aprovado para construção de Aparthotel | Sintra",
    "** VENDIDA ** Azeitão | T4 | 180mtr - Patricia Taylor Imóveis | Moradias de Luxo em Portugal",
    "Moradia T3 vendida em projeto - Resort em Óbidos.",
])
def test_a_title_that_says_sold_or_reserved_is_recognised(title):
    assert sold_from_title(title) is True


@pytest.mark.parametrize("title", [
    # The word is a SALE CONDITION, not a completed sale. All nine of these are live listings.
    "2 MORADIAS REMODELADAS COM 2 PISOS, VENDIDAS EM CONJUNTO",
    "1 de 2 Moradias com 2 Pisos - Vendidas em Conjunto - AJUDA - INVEST...",
    "2 moradias ligadas entre si vendidas em conjunto - Arrendadas",
    "6 lotes de Terreno com 7830mts2 cada, vendidos em conjunto",
    "T1 vendido no estado atual",
    "T2 vendido no estado actual",
    "Excelente T4 com uma área útil de ( 130 m2 ), dois W/C, è vendido com todo o recheio",
    # "preservad" literally contains "reservad": the lookbehind is what keeps these out.
    "Apartamento T3 com traça clássica preservada em Alfama",
    "Moradia T4 com fachada preservada e jardim",
    # A secluded setting, which is a selling point. This is why descriptions are not scanned at all:
    # all 14 sampled description hits read like this, and filtering them would have hidden 65 properties.
    "Moradia T3 em ambiente reservado e tranquilo",
    None,
    "",
])
def test_a_title_that_only_looks_sold_stays_visible(title):
    assert sold_from_title(title) is False


def test_the_canonical_listing_downgrades_itself_so_no_adapter_can_forget():
    from app.domain.listing import CanonicalListing

    base = dict(source_key="imovirtual", external_id="1", url="https://x/1")
    assert CanonicalListing(**base, title="Apartamento T2 Vendido Lisboa").status == "sold"
    assert CanonicalListing(**base, title="Apartamento T2 em Lisboa").status == "active"
    assert CanonicalListing(**base, title="T1 vendido no estado atual").status == "active"


def test_a_status_the_source_reported_is_never_overwritten():
    """RE/MAX `isSold` and eGO `soldout` already arrive typed; the title rule must not fight them."""
    from app.domain.listing import CanonicalListing

    base = dict(source_key="remax", external_id="1", url="https://x/1")
    assert CanonicalListing(**base, title="Moradia T4 em Sintra", status="sold").status == "sold"
    assert CanonicalListing(**base, title="Apartamento T2 Vendido Lisboa", status="removed").status == "removed"


def test_a_sold_listing_fails_matching_which_is_what_hides_it():
    """No new filter is needed: `matching.evaluate` already requires an active listing."""
    r = evaluate(listing(status="sold", features={"exterior": {"value": True, "tier": "confirmed"}}),
                 C(), ROSA_ZONES)
    assert not r.ok and "ativo" in r.unsatisfied


@pytest.mark.parametrize("title", [
    "Moradia T3 em ambiente reservado e tranquilo",
    "Apartamento com espaço reservado para refeições",
    "Moradia T4 com zona reservada e piscina",
    "T3 com varanda reservada sobre o jardim",
    "Apartamento com utilização reservada do logradouro",
])
def test_reservado_meaning_secluded_is_not_a_sale_status(title):
    """"Reservado" also means secluded, and then it describes a noun, not the sale. No production title
    does this today, but every sampled DESCRIPTION hit did, so a title like these is plainly plausible."""
    assert sold_from_title(title) is False


def test_a_secluded_phrase_does_not_mask_a_real_marker_elsewhere():
    """Removing the phrase must not let a genuine marker slip past in the same title."""
    assert sold_from_title("RESERVADO: Moradia T3 em ambiente reservado") is True
    assert sold_from_title("Apartamento T2 Vendido Lisboa, zona reservada") is True
