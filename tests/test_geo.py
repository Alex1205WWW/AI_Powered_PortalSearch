from app.geo.resolve import ancestors, child_code, is_within, level_of, slugify


def test_slugify_strips_accents_and_spaces():
    assert slugify("Sobral de Monte Agraço") == "sobral-de-monte-agraco"
    assert slugify("Setúbal") == "setubal"
    assert slugify("  Vila Franca de Xira ") == "vila-franca-de-xira"


def test_is_within_is_a_path_prefix_test():
    assert is_within("lisboa/oeiras/alges", "lisboa/oeiras")
    assert is_within("lisboa/oeiras", "lisboa")
    assert is_within("lisboa/oeiras", "lisboa/oeiras")
    assert not is_within("lisboa/oeiras", "lisboa/lisboa")
    assert not is_within("lisboa/lisboa", "lisboa/lis")  # no partial-segment matches


def test_levels_and_ancestors():
    assert level_of("lisboa") == "district"
    assert level_of("lisboa/oeiras") == "council"
    assert level_of("lisboa/oeiras/alges") == "parish"
    assert ancestors("lisboa/oeiras/alges") == ["lisboa", "lisboa/oeiras"]
    assert child_code("lisboa/oeiras", "Algés") == "lisboa/oeiras/alges"


# --- free-text zone resolution (Day 5) ---------------------------------------------------------
# Agency sites publish no structured geo: eGO's only location is the listing title. These are real titles
# and slugs captured on 2026-10-31.

from app.geo.resolve import candidate_slugs, choose_place


def test_candidate_slugs_keeps_multi_word_places_whole():
    found = candidate_slugs("Moradia T3 em Vila Franca de Xira")
    assert "vila-franca-de-xira" in found          # the real place...
    assert "xira" in found                        # ...and its parts, so the longest match can win
    assert "vila-nova-da-barquinha" not in found


def test_candidate_slugs_reads_a_real_ego_title():
    found = candidate_slugs("Apartamento T3 para venda em Alcântara - Lisboa")
    assert {"alcantara", "lisboa"} <= found


def test_a_parish_wins_over_its_own_ancestors():
    assert choose_place([("lisboa", "district"), ("lisboa/lisboa", "council"),
                         ("lisboa/lisboa/alcantara", "parish")]) == ("lisboa/lisboa/alcantara", "parish")


def test_lisboa_alone_stays_a_district_and_is_never_promoted_to_the_council():
    """"Lisboa" names both. Answering the council would let a Loures property be confirmed into Raquel's
    Lisboa-council search; answering the district makes `matching.zone_verdict` say `unknown` instead, which
    shows as "possível" and is recoverable."""
    assert choose_place([("lisboa", "district"), ("lisboa/lisboa", "council")]) == ("lisboa", "district")


def test_a_named_council_disambiguates_a_repeated_parish_name():
    matches = [("lisboa/lisboa/santo-antonio", "parish"), ("setubal/setubal/santo-antonio", "parish"),
               ("lisboa/lisboa", "council")]
    assert choose_place(matches) == ("lisboa/lisboa/santo-antonio", "parish")


def test_an_undecidable_parish_name_resolves_to_nothing_rather_than_a_guess():
    """A wrong zone files a Setúbal flat under Lisboa and shows it as a confirmed match. An unresolved zone
    only shows it as "possível"."""
    assert choose_place([("lisboa/lisboa/santo-antonio", "parish"),
                         ("setubal/setubal/santo-antonio", "parish")]) == (None, None)


def test_nothing_recognised_is_nothing_claimed():
    assert choose_place([]) == (None, None)
    assert candidate_slugs(None, "") == set()
