"""Delivery bookkeeping for notifications, and the index the rematch preload query needs

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04

Two unrelated-looking changes, both from the 2026-11-04 test-and-optimise pass
(`Milestone1/Plan_TestOptimize.md`, items T1.1 and T1.6).

1. `notifications.attempts` / `last_error` / `failed_at` - delivery is now observable and bounded.

   Before this, a Telegram failure left the row unsent, logged one ERROR line, and was retried every two
   minutes forever: 738 such attempts in 24 hours while the production bot was blocked, with nothing in the
   panel, the digest or the database saying that her alerts were not arriving. During a 7-day acceptance
   test that is the difference between a number we can defend and no evidence at all.

   `attempts` counts tries so the retry can stop being infinite; `last_error` keeps the reason where it can
   be read without `docker compose logs`; `failed_at` is the give-up marker. A row with `failed_at` set
   leaves the pending queue but stays on the record, which matters because the queue is processed in
   `created_at` order - one permanently undeliverable alert must not block every alert queued behind it.

   All three are nullable or defaulted, so existing rows need no backfill: 421 rows keep `attempts = 0`,
   which is exactly true of them, since nothing was counting before.

2. `ix_property_matches_criteria_id` - `matches.existing_by_property()` reads every stored match for one
   criteria in a single query, which is what removed the last per-property SELECT from the rematch loop
   (12 921 queries down to 16). That query had no index: a sequential scan of `property_matches`, 0.32 ms
   at today's 1 754 rows, growing linearly with every buyer added against a contract that promises 50.
   The column is also the referencing side of a `criteria` foreign key with ON DELETE CASCADE, so deleting
   a buyer's criteria scans the table too. One index serves both.

Additive and reversible; `downgrade()` is exact and no data is lost in either direction.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('notifications', sa.Column('last_error', sa.Text(), nullable=True))
    op.add_column('notifications', sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_property_matches_criteria_id', 'property_matches', ['criteria_id'])


def downgrade() -> None:
    op.drop_index('ix_property_matches_criteria_id', table_name='property_matches')
    op.drop_column('notifications', 'failed_at')
    op.drop_column('notifications', 'last_error')
    op.drop_column('notifications', 'attempts')
