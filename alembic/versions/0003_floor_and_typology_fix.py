"""listings.floor + Imovirtual typology correction + source confidence

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28

Why: Imovirtual `roomsNumber` counts divisions, not bedrooms (verified live 2026-10-28: slug T2 -> THREE). Day 2 stored
T+1, so this revision rewrites the stored Imovirtual `rooms` to the typology and the adapter now subtracts one.
After upgrading run `python -m app.cli rematch` so `property_matches` reflect the corrected typologies (silent, no alerts).
`floor` is backfilled from the raw payload already stored, so no re-crawl is needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IMOVIRTUAL = "(SELECT id FROM sources WHERE key = 'imovirtual')"
FLOOR_CASE = """CASE raw->>'floorNumber'
    WHEN 'CELLAR' THEN -1 WHEN 'GROUND' THEN 0 WHEN 'FIRST' THEN 1 WHEN 'SECOND' THEN 2 WHEN 'THIRD' THEN 3
    WHEN 'FOURTH' THEN 4 WHEN 'FIFTH' THEN 5 WHEN 'SIXTH' THEN 6 WHEN 'SEVENTH' THEN 7 WHEN 'EIGHTH' THEN 8
    WHEN 'NINTH' THEN 9 WHEN 'TENTH' THEN 10 WHEN 'ABOVE_TENTH' THEN 11 END"""


def upgrade() -> None:
    op.add_column('listings', sa.Column('floor', sa.Integer(), nullable=True))
    op.execute(f"UPDATE listings SET rooms = GREATEST(rooms - 1, 0) WHERE rooms IS NOT NULL AND source_id = {IMOVIRTUAL}")
    op.execute(f"UPDATE listings SET floor = {FLOOR_CASE} WHERE source_id = {IMOVIRTUAL} AND raw->>'floorNumber' IS NOT NULL")
    op.execute("UPDATE sources SET confidence = 'full' WHERE key IN ('imovirtual', 'remax') AND confidence IS NULL")
    op.execute("UPDATE sources SET confidence = 'partial' WHERE key = 'idealista_email' AND confidence IS NULL")


def downgrade() -> None:
    op.execute(f"UPDATE listings SET rooms = rooms + 1 WHERE rooms IS NOT NULL AND source_id = {IMOVIRTUAL}")
    op.drop_column('listings', 'floor')
