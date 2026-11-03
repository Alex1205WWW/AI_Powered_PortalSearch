"""Stop re-fetching photo URLs that will never hash

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04

`warm_hashes` runs every 15 minutes and looks for `phash IS NULL`, with no memory of having already tried.
36 photo URLs on production are permanently dead - a withdrawn Supabase bucket, an OLX CDN path that 404s,
two Idealista images - so every tick re-fetched all 36 and reported `failed: 36, remaining: 36`, for ever.

The waste is small (144 pointless requests an hour). The reason to fix it is the error floor: a constant
background of 36 failures is exactly where a *new*, real breakage hides, and the source-health canary is
the thing meant to catch that. It is also unkind to the hosts being re-asked for a file that is gone.

`phash_failures` counts attempts per image so the warmer can give up, while leaving `phash` NULL - the
image genuinely has no hash, and dedup must keep treating it as unknown rather than as a hash of zero.
Scoped per image URL row, not per listing, matching how the hash cache already works.

Additive with a server default on 153 730 rows, so the rewrite is a fast one and existing rows start at 0,
which is true of them: nothing was counting before. `downgrade()` is exact.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('listing_images',
                  sa.Column('phash_failures', sa.SmallInteger(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('listing_images', 'phash_failures')
