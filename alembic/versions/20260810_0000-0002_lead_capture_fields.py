"""Add the capture fields the restructured funnel collects.

Every column is nullable with no default, which makes this additive and safe to
run against a table that already holds leads: existing rows simply carry NULL
for fields that were never asked for.

Revision ID: 0002_lead_capture_fields
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_lead_capture_fields"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


#: (column, type). Kept as data so upgrade and downgrade cannot drift apart -
#: a downgrade that misses a column leaves the schema in a state no migration
#: describes, which is worse than not having one.
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[str]], ...] = (
    ("contact_phone", sa.String(length=32)),
    ("email", sa.String(length=255)),
    ("enrolled_course", sa.String(length=120)),
    ("profession", sa.String(length=255)),
    ("issue_type", sa.String(length=64)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("leads", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("leads", name)
