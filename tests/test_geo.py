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
