"""Two indexes for the hot paths: the zone prefix match, and the photo-hash cache lookup

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-04

Both were found by reading `pg_stat_user_tables` on the live database (34 283 listings, 153 630 images),
not by guessing: `listings` had taken 5 710 sequential scans reading 104 588 968 rows, and
`listing_images` 5 039 scans reading 85 729 606 rows. Each index removes one of those two.

1. `ix_listings_active_geo_prefix` - `geo_code` with **text_pattern_ops**, partial on the rows every
   caller already filters to (`status='active' AND property_id IS NOT NULL`).

   Zones are hierarchical slugs (`lisboa/lisboa/alvalade`), so "in this council" is written
   `geo_code = :c OR geo_code LIKE :c || '/%'` - in `dedup.candidates()`, which runs for EVERY newly
   ingested listing, and in `runs._zone_filter()`, which runs for every criteria on every rematch.
   This database is `en_US.utf8`, and under any non-C collation a plain btree CANNOT serve `LIKE 'x/%'`:
   the operator class's sort order is not the byte order the prefix scan needs. So the existing
   `ix_listings_geo_type_rooms` was unusable for that half of the OR and the planner fell back to a full
   scan of all 34 283 rows - which is exactly what the counters above recorded. `text_pattern_ops` sorts
   by byte, which is what makes the prefix range `>=~ 'x/' AND <~ 'x0'` indexable.

   Measured on production, heaviest council (`lisboa/lisboa`, 5 253 active listings):
     zone filter        29.7 ms -> 5.6 ms   (5 977 -> 2 842 buffers)
     dedup candidates   22.6 ms -> 4.4 ms   (5 980 -> 1 652 buffers)
   The equality half of the OR is served by the same index, so nothing else is needed.

2. `ix_listing_images_url` - `properties.ensure_hashes()` and `warm_hashes()` both ask
   "has this exact photo URL already been hashed?" (`WHERE url = :u AND phash IS NOT NULL`). There was no
   index on `url` at all, so Postgres scanned `ix_listing_images_phash` and discarded all 61 768 hashed
   rows to find one URL: 3.5 ms and 198 buffers per image, on a table that grows with every crawl.
   Measured after: 0.3 ms.

   USING HASH, not btree. The lookup is equality and nothing else - there is no prefix match, no range and
   no ORDER BY on a photo URL, and never will be, because the question is only ever "this exact URL". A
   hash index stores the 32-bit hash instead of the string, and photo URLs are long, so the difference is
   not marginal: measured on this database, btree 20 MB against a 23 MB table, hash 4.1 MB, with the same
   lookup time (0.344 ms btree vs 0.305 ms hash - one sample each, so call it equal). Hash indexes have
   been WAL-logged and crash-safe since Postgres 10; this server runs 16.15. They cannot serve a UNIQUE
   constraint, which this column neither has nor wants: the same photo URL legitimately appears on several
   listings, and that is exactly what makes the cache worth having.

Indexes only, so `downgrade()` is exact and no data can be lost either way. Together they cost about
4.4 MB and are maintained on insert, which is the right trade when the read happens once per ingested
listing and the write happens once.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_listings_active_geo_prefix ON listings (geo_code text_pattern_ops) "
        "WHERE status = 'active' AND property_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_listing_images_url ON listing_images USING hash (url)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_listing_images_url")
    op.execute("DROP INDEX IF EXISTS ix_listings_active_geo_prefix")
