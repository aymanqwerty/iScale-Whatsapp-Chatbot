"""Flag contacts who have sent a payment screenshot.

One nullable timestamp, so every existing row is valid the moment this lands.
Null means "nothing to verify", which is the correct reading of every row that
predates the feature.

Revision ID: 0005_payment_proof
Revises: 0004_contact_blocking
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_payment_proof"
down_revision = "0004_contact_blocking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("payment_proof_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "payment_proof_at")
