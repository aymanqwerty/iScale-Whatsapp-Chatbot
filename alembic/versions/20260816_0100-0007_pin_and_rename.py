"""Let agents pin a conversation and relabel a contact.

`alias` is kept apart from `name` on purpose. `name` feeds `User.display_name`,
which the bot greets people by and prefills bookings with - so writing an
agent's label there would put an internal note in front of the customer.

Both columns are nullable, so existing rows are correct untouched: no alias and
not pinned is exactly right for every contact that predates this.

Revision ID: 0007_pin_and_rename
Revises: 0006_message_media
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_pin_and_rename"
down_revision = "0006_message_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("alias", sa.String(length=120), nullable=True))
    op.add_column(
        "users", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "pinned_at")
    op.drop_column("users", "alias")
