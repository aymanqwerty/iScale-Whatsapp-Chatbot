"""Let an agent block a contact outright.

Additive and server-defaulted, like 0003, so every existing row is valid the
moment this lands: no backfill, and no window where `blocked` is NULL and the
inbound path has to guess whether to answer.

No index on `blocked`. The inbound path never searches by it - it already holds
the `User` row, fetched by the unique index on `phone`, and only reads the flag
off it. An index here would be written on every contact and read by nothing.

Revision ID: 0004_contact_blocking
Revises: 0003_agent_handover
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_contact_blocking"
down_revision = "0003_agent_handover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "blocked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("users", sa.Column("blocked_by", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "blocked_by")
    op.drop_column("users", "blocked_at")
    op.drop_column("users", "blocked")
