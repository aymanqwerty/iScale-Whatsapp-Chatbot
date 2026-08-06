"""Data-access layer.

Services depend on repositories, never on SQLAlchemy queries directly. That
boundary is what makes it possible to swap the conversation store for Redis
later without touching a single line of bot logic.
"""

from app.repositories.conversation_repository import ConversationRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    "ConversationRepository",
    "LeadRepository",
    "MessageRepository",
    "UserRepository",
]
