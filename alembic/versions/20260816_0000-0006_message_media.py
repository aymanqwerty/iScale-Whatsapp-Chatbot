"""Store payment screenshots alongside the message that carried them.

Cloud API keeps media on Meta's servers behind the access token and expires it,
and there is no WhatsApp app on our side to open a picture in - so a screenshot
that is not downloaded and stored is unreachable the moment it arrives.

Both columns are nullable and only populated for payment proofs, so existing
rows are valid untouched. Postgres TOASTs a bytea this size out of the main
heap, which keeps the ~99% of NULL rows exactly as cheap as they were.

Revision ID: 0006_message_media
Revises: 0005_payment_proof
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_message_media"
down_revision = "0005_payment_proof"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("media_data", sa.LargeBinary(), nullable=True))
    op.add_column(
        "messages", sa.Column("media_mime", sa.String(length=80), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("messages", "media_mime")
    op.drop_column("messages", "media_data")
