"""Handover state for the agent console.

Both columns are additive and carry server defaults, so existing rows are valid
the moment the migration lands - no backfill, no window where `bot_paused` is
NULL and the bot has to guess.

Revision ID: 0003_agent_handover
Revises: 0002_lead_capture_fields
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_agent_handover"
down_revision = "0002_lead_capture_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "bot_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "paused_at")
    op.drop_column("users", "bot_paused")
