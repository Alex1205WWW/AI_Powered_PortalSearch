"""Agency discovery: directory identity, liveness counters, off-portal ranking, catalogue-size guard

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31

Day 5 (`Milestone1/Plan_Day5.md`). Five columns and one unique index; no new table, because `agencies` and
`sources` already carry everything else the agency reader needs.

- `agencies.imovirtual_id` is new and UNIQUE: the directory UUID is the real identity, since `name_norm` is
  not (two different agencies can normalise to the same string).
  An earlier draft of this revision tried to reuse `source_urn` for that and **failed on the live database**:
  `source_urn` already holds the advertiser's CRM, which is shared by design - `urn:partner:ego` on 132 rows,
  `urn:site:local` on 65, `urn:partner:maxwork` on 55, `urn:partner:century21` on 54. That column is left
  exactly as it is, and it turns out to be a free platform fingerprint straight from the portal feed: an
  agency reported as `urn:partner:ego` is known to run eGO before its own site is fetched once.
- `agencies.active_ads_rent_count` separates a dead account (sell=0 AND rent=0 - 1 480 of the 1 502 zero-sell
  agencies measured 2026-10-31) from a rental-only business (Uniplaces: sell=0, rent=8 130). Both are skipped,
  for different reasons, and the panel should be able to say which.
- `agencies.checked_at` remembers that we looked and found no website, so the funnel does not re-fetch
  1 502 profiles on every tick.
- `agencies.offportal_ratio` / `listings_seen` rank the daily watch budget by measured off-portal yield.
- `sources.last_catalogue_size` feeds the removal guard: eGO sitemaps carry no <lastmod>, so removal is
  detected by set-difference, and a short or truncated sitemap must never retire a whole catalogue.

`imovirtual_id` is NULL on every existing row, and NULLs do not collide under a UNIQUE index in Postgres, so
it is safe to add without a backfill; the next directory crawl fills it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agencies', sa.Column('imovirtual_id', sa.String(length=40), nullable=True))
    op.add_column('agencies', sa.Column('active_ads_rent_count', sa.Integer(), nullable=True))
    op.add_column('agencies', sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agencies', sa.Column('offportal_ratio', sa.Float(), nullable=True))
    op.add_column('agencies', sa.Column('listings_seen', sa.Integer(), nullable=True))
    op.add_column('sources', sa.Column('last_catalogue_size', sa.Integer(), nullable=True))
    op.create_unique_constraint('uq_agencies_imovirtual_id', 'agencies', ['imovirtual_id'])


def downgrade() -> None:
    op.drop_constraint('uq_agencies_imovirtual_id', 'agencies', type_='unique')
    op.drop_column('sources', 'last_catalogue_size')
    op.drop_column('agencies', 'listings_seen')
    op.drop_column('agencies', 'offportal_ratio')
    op.drop_column('agencies', 'checked_at')
    op.drop_column('agencies', 'active_ads_rent_count')
    op.drop_column('agencies', 'imovirtual_id')
