"""Dedup rules on ~20 pairs: synthetic photos (no real images stored) plus the real sibling cases found in the DB
on 2026-10-28 (RED BUILDING x4, Realty Art townhouses x3, eXp re-listing). Precision first: zero false merges."""
import io
import random

import pytest
from PIL import Image, ImageDraw

from app.domain import dedup
from app.domain.dedup import Decision, Side, decide

IMO, REMAX, MAIL = 1, 2, 3  # source ids


def synth_photo(seed: int, size=(640, 480)) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", size, (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rnd.randint(0, size[0] - 1), rnd.randint(0, size[1] - 1)
        draw.rectangle([x0, y0, rnd.randint(x0, size[0]), rnd.randint(y0, size[1])], fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    return img


def jpeg(img: Image.Image, width: int, quality: int) -> bytes:
    out = io.BytesIO()
    img.resize((width, int(img.height * width / img.width))).save(out, "JPEG", quality=quality)
    return out.getvalue()


def photo_hash(seed: int, width: int = 314, quality: int = 80) -> int:
    return dedup.phash_of(jpeg(synth_photo(seed), width, quality))


def side(lid, prop, source=IMO, council="lisboa/lisboa", estate="apartamento", rooms=2, area=60.0, price=350_000, **kw) -> Side:
    return Side(listing_id=lid, property_id=prop, source_id=source, council=council, estate_type=estate, rooms=rooms, area=area, price=price, **kw)


# --- hashing -------------------------------------------------------------------------------------

def test_phash_is_stable_across_portal_re_encodings_and_separates_different_photos():
    a_full, a_small, a_tiny = photo_hash(1, 800, 90), photo_hash(1, 314, 60), photo_hash(1, 160, 40)
    assert dedup.hamming(a_full, a_small) <= 4 and dedup.hamming(a_full, a_tiny) <= 6
    others = [photo_hash(seed) for seed in range(2, 22)]
    assert min(dedup.hamming(a_full, h) for h in others) > dedup.HAMMING_MAX


def test_signed_storage_roundtrip():
    for value in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1, 0xAD2D54D34E5683CA):
        signed = dedup.to_signed(value)
        assert -(1 << 63) <= signed < (1 << 63) and dedup.to_unsigned(signed) == value


def test_matching_pairs_is_one_to_one():
    h = photo_hash(5)
    assert dedup.matching_pairs([h, h, h], [h]) == 1
    assert dedup.matching_pairs([photo_hash(5), photo_hash(6)], [photo_hash(6, 200, 50), photo_hash(5, 200, 50)]) == 2
    assert dedup.required_pairs([1], [1, 2, 3]) == 1 and dedup.required_pairs([1, 2], [1, 2, 3]) == 2


# --- blocking key ----------------------------------------------------------------------------------

def test_blocking_key_tolerances_and_unknowns():
    a = side(1, 10)
    assert dedup.compatible(a, side(2, 11, area=62.9, price=360_000))         # +4.8 % area, +2.9 % price
    assert not dedup.compatible(a, side(2, 11, area=64.0))                    # +6.7 % area
    assert not dedup.compatible(a, side(2, 11, price=365_000))                # +4.3 % price
    assert not dedup.compatible(a, side(2, 11, rooms=3))
    assert not dedup.compatible(a, side(2, 11, council="lisboa/oeiras"))
    assert not dedup.compatible(a, side(2, 11, estate="moradia"))
    assert dedup.compatible(a, side(2, 11, rooms=None, area=None))            # an email card that only says the price
    assert dedup.compatible(a, side(2, 11, price=None, hide_price=True))      # "preço sob consulta" does not block
    assert not dedup.compatible(a, side(2, 11, council=None))
    assert dedup.council_of("lisboa/lisboa/alfama") == "lisboa/lisboa" and dedup.council_of("lisboa") is None


# --- identity rules -------------------------------------------------------------------------------

def test_rule_a_same_agency_and_reference_merges_even_on_the_same_source():
    a = side(1, 10, agency_norm="remax vantagem", reference="125011292-32")
    b = side(2, None, agency_norm="remax vantagem", reference="125011292/32")  # punctuation differs
    assert decide(b, [a]) == Decision(10, dedup.RULE_REFERENCE)
    c = side(3, None, agency_norm="outra", reference="125011292-32")
    assert decide(c, [a]).rule == dedup.RULE_NONE  # same reference, different agency: not identifying


def test_rule_b_cross_source_photos_merge_the_same_flat():
    """Imovirtual listing + the same flat from an alert email (one photo, re-encoded) -> one property."""
    imo = side(1, 10, source=IMO, hashes=[photo_hash(1), photo_hash(2), photo_hash(3)])
    mail = side(2, None, source=MAIL, rooms=None, area=None, hashes=[photo_hash(1, 200, 50)])
    assert decide(mail, [imo]) == Decision(10, dedup.RULE_PHOTOS)
    remax = side(3, None, source=REMAX, hashes=[photo_hash(2, 500, 70), photo_hash(1, 500, 70), photo_hash(9)])
    assert decide(remax, [imo]) == Decision(10, dedup.RULE_PHOTOS)  # 2 of 3 photos match, order differs


def test_rule_b_needs_two_pairs_when_both_sides_have_several_photos():
    imo = side(1, 10, source=IMO, hashes=[photo_hash(1), photo_hash(2), photo_hash(3)])
    remax = side(2, None, source=REMAX, hashes=[photo_hash(1), photo_hash(7), photo_hash(8)])  # only the facade matches
    assert decide(remax, [imo]).rule == dedup.RULE_NONE


def test_rule_b_never_merges_within_the_same_source():
    """eXp Portugal, Imovirtual ids 840/841: same flat twice on one portal, 3 shared photos, 405k/400k. Stays visible twice
    rather than risking the sibling-unit pattern; only a shared reference would merge it."""
    a = side(840, 10, source=IMO, area=65, price=405_000, hashes=[photo_hash(1), photo_hash(2), photo_hash(3)])
    b = side(841, None, source=IMO, area=65, price=400_000, hashes=[photo_hash(1), photo_hash(2), photo_hash(3)])
    assert decide(b, [a]).rule == dedup.RULE_NONE


def test_red_building_four_sibling_units_never_collapse():
    """Century21 Rio / Rio 2, Santa Maria Maior: four T2 at 385-390k, 54-55 m2, sharing building photos. On Imovirtual
    each keeps its own property (same source). An alert email for one of them matches all four -> ambiguous -> separate."""
    shared = [photo_hash(1), photo_hash(2)]
    units = [side(1151 + i, 100 + i, source=IMO, area=54 + (i == 3), price=385_000 + 5_000 * (i == 3), hashes=shared + [photo_hash(10 + i)]) for i in range(4)]
    for i in range(1, 4):
        assert decide(units[i], units[:i]).rule == dedup.RULE_NONE
    mail = side(2000, None, source=MAIL, area=54, price=385_000, hashes=[photo_hash(1, 200, 50), photo_hash(2, 200, 50)])
    verdict = decide(mail, units)
    assert verdict.rule == dedup.RULE_AMBIGUOUS and verdict.property_id is None


def test_realty_art_three_identical_new_townhouses_stay_apart():
    """Loures, three "Moradia Nova T3+1" at 625k sharing all renders, not flagged as a development by the portal."""
    renders = [photo_hash(21), photo_hash(22), photo_hash(23)]
    a = side(79, 1, estate="moradia", rooms=3, area=143, price=625_000, hashes=renders)
    b = side(109, 2, estate="moradia", rooms=3, area=136, price=625_000, hashes=renders)
    c = side(111, None, estate="moradia", rooms=3, area=136, price=625_000, hashes=renders)
    assert decide(c, [a, b]).rule == dedup.RULE_NONE  # same source
    remax = side(5000, None, source=REMAX, estate="moradia", rooms=3, area=136, price=625_000, hashes=renders)
    assert decide(remax, [a, b]).rule == dedup.RULE_AMBIGUOUS


def test_different_floors_in_one_building_stay_apart_even_with_shared_photos():
    a = side(1, 10, source=IMO, floor=2, hashes=[photo_hash(1), photo_hash(2), photo_hash(3)])
    b = side(2, None, source=REMAX, floor=4, hashes=[photo_hash(1), photo_hash(2), photo_hash(3)])
    assert decide(b, [a]).rule == dedup.RULE_NONE
    c = side(3, None, source=REMAX, floor=None, hashes=[photo_hash(1), photo_hash(2), photo_hash(3)])
    assert decide(c, [a]) == Decision(10, dedup.RULE_PHOTOS)  # unknown floor does not contradict


def test_new_developments_only_merge_by_reference():
    a = side(1, 10, source=IMO, is_new_development=True, hashes=[photo_hash(1), photo_hash(2)])
    b = side(2, None, source=REMAX, hashes=[photo_hash(1), photo_hash(2)])
    assert decide(b, [a]).rule == dedup.RULE_NONE
    b.agency_norm = a.agency_norm = "promotor x"
    b.reference = a.reference = "LOTE-7"
    assert decide(b, [a]).rule == dedup.RULE_REFERENCE


def test_no_photos_no_candidates_means_own_property():
    assert decide(side(1, None), []) == Decision(None, dedup.RULE_NONE)
    assert decide(side(1, None, source=MAIL), [side(2, 10, source=IMO)]).rule == dedup.RULE_NONE


def test_reference_beats_photos_and_picks_the_oldest_cluster():
    a = side(1, 10, source=IMO, agency_norm="ag", reference="R1", hashes=[photo_hash(1), photo_hash(2)])
    b = side(2, 11, source=REMAX, agency_norm="ag", reference="R1", hashes=[photo_hash(5), photo_hash(6)])
    c = side(3, None, source=MAIL, agency_norm="ag", reference="R1", hashes=[photo_hash(1, 200, 50)])
    assert decide(c, [b, a]) == Decision(10, dedup.RULE_REFERENCE)


@pytest.mark.parametrize("bad", [b"not an image", b""])
def test_phash_of_rejects_non_images(bad):
    with pytest.raises(Exception):
        dedup.phash_of(bad)


# --- an unstorable price must not kill the run (2026-11-02) --------------------------------------

def test_a_price_too_large_for_the_column_becomes_sob_consulta():
    """sintraneves.com lists a genuine 7 500 000 000 EUR industrial portfolio. `listings.price` is a
    Postgres `integer`, so storing it raised NumericValueOutOfRange, killed the transaction and left all
    279 of that site's listings unstored with the source permanently `degraded`."""
    from app.domain.listing import PRICE_MAX, storable_price

    assert PRICE_MAX == 2_147_483_647
    assert storable_price(None) is None
    assert storable_price(650_000) == 650_000
    assert storable_price(69_600_000) == 69_600_000        # the largest real listing in the database
    assert storable_price(PRICE_MAX) == PRICE_MAX
    assert storable_price(PRICE_MAX + 1) is None
    assert storable_price(7_500_000_000) is None           # the sintraneves listing
    assert storable_price(-7_500_000_000) is None
    assert storable_price(650_000.0) == 650_000            # floats from JSON-LD still round-trip


# --- a dead photo URL must not be re-fetched for ever (T1.5, 2026-11-04) ------------------------

def test_the_hash_warmer_gives_up_after_a_bounded_number_of_attempts():
    """36 permanently dead URLs (a withdrawn Supabase bucket, an OLX CDN path that 404s) were re-fetched
    every 15 minutes with no memory of having tried: `failed: 36, remaining: 36`, every tick, for ever.
    The waste is small; the reason to cap it is that a constant floor of 36 failures is exactly where a
    new, real breakage hides from the canary."""
    from app.services.properties import MAX_HASH_FAILURES

    assert MAX_HASH_FAILURES == 3

    import inspect

    from app.services import properties as prop_svc

    warm = inspect.getsource(prop_svc.warm_hashes)
    assert "phash_failures < MAX_HASH_FAILURES" in warm, "given-up images must leave the query"
    assert "image.phash_failures = (image.phash_failures or 0) + 1" in warm, "the attempt must be counted"
    assert '"given_up"' in warm, "the operator needs the number it gave up on"

    ensure = inspect.getsource(prop_svc.ensure_hashes)
    assert "MAX_HASH_FAILURES" in ensure, "the ingest path must respect the cap too"


def test_giving_up_leaves_the_hash_unknown_not_zero():
    """`phash` stays NULL: the image genuinely has no hash, and the photo rule must keep reading that as
    unknown. A sentinel of 0 would be a real hash value and could match another image."""
    import inspect

    from app.services import properties as prop_svc

    warm = inspect.getsource(prop_svc.warm_hashes)
    # the failure branch increments the counter and continues; it never assigns phash
    failure_branch = warm.split("if value is None:")[1].split("image.phash, done")[0]
    assert "image.phash =" not in failure_branch and "image.phash," not in failure_branch
