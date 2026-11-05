"""Clear the RE/MAX street that is really a boolean flag

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05

The adapter read `publicAddress` as the street. It is a BOOLEAN ("may this address be shown?"), so 9 842
RE/MAX listings - 56 % of every listing in the database that has a street at all - stored the text "true",
and the Imóveis page printed it to her underneath the zone as if it were the address.

Found by looking at a screenshot of the real panel while capturing the manual's images. No test caught it
(the adapter tests never asserted the street), no crawl error was raised (the value is a valid string), and
the field-fill canary was satisfied (the field was filled - with nonsense).

The code fix is in `sources/remax.py` (`_public_street`, which also honours `publicAddress: False`) plus a
guard in `CanonicalListing.__post_init__` so no future adapter can store a non-text street. This migration
is for the rows already written: a sweep only rewrites a listing when it is re-fetched, and the RE/MAX
backfill has finished, so without it those 9 842 rows would keep showing "true" for weeks.

Scoped as narrowly as the evidence allows: only RE/MAX, and only the exact values a boolean coerces to.
A real street is never "true" or "false", and this touches nothing else. The correct address comes back on
each listing's next crawl, from the field that actually holds it.

`downgrade()` is deliberately a no-op: putting a known-wrong value back is not a restoration. The data lost
is the string "true", which carried no information.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE listings SET street = NULL
         WHERE street IN ('true', 'false', 'True', 'False')
           AND source_id IN (SELECT id FROM sources WHERE key = 'remax')
    """)


def downgrade() -> None:
    """No-op: the cleared value was a flag misread as an address, not data to restore."""
