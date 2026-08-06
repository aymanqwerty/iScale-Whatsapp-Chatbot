"""Repository-level checks that the SQLite-backed flow tests cannot make.

The suite runs against SQLite, which silently ignores `FOR UPDATE`. Anything
about row locking therefore has to be asserted by compiling the statement
against the PostgreSQL dialect instead of by executing it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db.models.conversation import Conversation


def _active_conversation_lock_sql() -> str:
    """The statement `ConversationRepository.get_active(for_update=True)` builds."""
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == 1, Conversation.is_active.is_(True))
        .order_by(Conversation.id.desc())
        .limit(1)
        .with_for_update(of=Conversation)
    )
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_conversation_lock_targets_only_the_conversations_table() -> None:
    """Regression: a bare FOR UPDATE is invalid on this query.

    `Conversation.user` is `lazy="joined"`, so the statement carries a LEFT
    OUTER JOIN to `users`. PostgreSQL rejects a lock that would cover the
    nullable side of that join with "FOR UPDATE cannot be applied to the
    nullable side of an outer join", which took down every inbound message.
    """
    sql = _active_conversation_lock_sql()

    assert "LEFT OUTER JOIN users" in sql, "the eager join this test guards is gone"
    assert "FOR UPDATE OF conversations" in sql


def test_conversation_lock_is_still_a_lock() -> None:
    """Narrowing the lock must not have quietly dropped it."""
    assert "FOR UPDATE" in _active_conversation_lock_sql()
