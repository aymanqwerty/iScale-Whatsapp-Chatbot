"""Initial schema: users, conversations, messages, leads

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01 00:00:00

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are stored as VARCHAR rather than native PostgreSQL types: adding a new
# conversation state then needs no ALTER TYPE, which keeps deploys simple.
_CONVERSATION_STATES = (
    "START", "MAIN_MENU", "COURSE_SELECTION", "COURSE_QNA", "GENERAL_QNA",
    "POST_SALES", "SUPPORT_QUERY", "SUPPORT_CALLBACK", "ASK_CALLBACK",
    "ASK_NAME", "ASK_CALLBACK_TIME", "ASK_REMARKS", "LEAD_CREATED", "END",
)
_LEAD_TYPES = ("PRE_SALES", "POST_SALES")
_LEAD_STATUSES = ("NEW", "CONTACTED", "QUALIFIED", "CONVERTED", "LOST")
_SYNC_STATUSES = ("PENDING", "SYNCED", "FAILED", "SKIPPED")
_SENDERS = ("USER", "BOT", "SYSTEM")


def _state_enum(name: str) -> sa.Enum:
    return sa.Enum(*_CONVERSATION_STATES, native_enum=False, length=32, name=name)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("profile_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("phone", name=op.f("uq_users_phone")),
    )
    op.create_index(op.f("ix_users_phone"), "users", ["phone"], unique=True)

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("current_state", _state_enum("conversationstate"), nullable=False),
        sa.Column("current_course", sa.String(length=64), nullable=True),
        sa.Column(
            "lead_type",
            sa.Enum(*_LEAD_TYPES, native_enum=False, length=16, name="leadtype"),
            nullable=True,
        ),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "last_activity_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_conversations_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        op.f("ix_conversations_user_id"), "conversations", ["user_id"], unique=False
    )
    # Supports the hot-path lookup: the caller's one open conversation.
    op.create_index(
        "ix_conversations_user_active", "conversations", ["user_id", "is_active"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column(
            "sender",
            sa.Enum(*_SENDERS, native_enum=False, length=16, name="messagesender"),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("wa_message_id", sa.String(length=128), nullable=True),
        sa.Column("state", _state_enum("conversationstate_msg"), nullable=True),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("wa_message_id", name=op.f("uq_messages_wa_message_id")),
    )
    op.create_index(
        op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False
    )
    # Unique index backs webhook de-duplication.
    op.create_index(
        op.f("ix_messages_wa_message_id"), "messages", ["wa_message_id"], unique=True
    )
    op.create_index(op.f("ix_messages_timestamp"), "messages", ["timestamp"], unique=False)

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column(
            "type",
            sa.Enum(*_LEAD_TYPES, native_enum=False, length=16, name="leadtype_lead"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*_LEAD_STATUSES, native_enum=False, length=16, name="leadstatus"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("interested_course", sa.String(length=120), nullable=True),
        sa.Column("preferred_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("preferred_time_raw", sa.String(length=255), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "sync_status",
            sa.Enum(*_SYNC_STATUSES, native_enum=False, length=16, name="syncstatus"),
            nullable=False,
        ),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"],
            name=op.f("fk_leads_conversation_id_conversations"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_leads_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
    )
    op.create_index(op.f("ix_leads_user_id"), "leads", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_leads_conversation_id"), "leads", ["conversation_id"], unique=False
    )
    op.create_index(op.f("ix_leads_type"), "leads", ["type"], unique=False)
    op.create_index(op.f("ix_leads_status"), "leads", ["status"], unique=False)
    op.create_index(op.f("ix_leads_phone"), "leads", ["phone"], unique=False)


def downgrade() -> None:
    op.drop_table("leads")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("users")
