"""Domain enumerations shared by the database models, services and bot."""

from __future__ import annotations

from enum import StrEnum


class ConversationState(StrEnum):
    """Nodes of the conversation state machine.

    The bot never asks the LLM "where are we in the flow?" - the answer is
    always this column, owned by the application.
    """

    START = "START"
    MAIN_MENU = "MAIN_MENU"

    # Pre-sales branch
    COURSE_SELECTION = "COURSE_SELECTION"
    COURSE_QNA = "COURSE_QNA"
    GENERAL_QNA = "GENERAL_QNA"

    # Post-sales branch
    POST_SALES = "POST_SALES"
    SUPPORT_QUERY = "SUPPORT_QUERY"
    SUPPORT_CALLBACK = "SUPPORT_CALLBACK"

    # Shared callback-capture branch
    ASK_CALLBACK = "ASK_CALLBACK"
    ASK_NAME = "ASK_NAME"
    ASK_CALLBACK_TIME = "ASK_CALLBACK_TIME"
    ASK_REMARKS = "ASK_REMARKS"
    LEAD_CREATED = "LEAD_CREATED"

    END = "END"


#: States in which a free-text message should be answered by the LLM.
QNA_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.COURSE_QNA,
        ConversationState.GENERAL_QNA,
        ConversationState.SUPPORT_QUERY,
    }
)

#: States that are part of capturing a callback request.
CALLBACK_CAPTURE_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.ASK_NAME,
        ConversationState.ASK_CALLBACK_TIME,
        ConversationState.ASK_REMARKS,
    }
)


class LeadType(StrEnum):
    """Which side of the funnel a lead came from."""

    PRE_SALES = "PRE_SALES"
    POST_SALES = "POST_SALES"


class LeadStatus(StrEnum):
    """Lifecycle of a lead once a counselor picks it up."""

    NEW = "NEW"
    CONTACTED = "CONTACTED"
    QUALIFIED = "QUALIFIED"
    CONVERTED = "CONVERTED"
    LOST = "LOST"


class SyncStatus(StrEnum):
    """Result of pushing a lead to the external CRM / sheet."""

    PENDING = "PENDING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class MessageSender(StrEnum):
    USER = "USER"
    BOT = "BOT"
    SYSTEM = "SYSTEM"


class MessageKind(StrEnum):
    """Inbound WhatsApp payload types we understand."""

    TEXT = "TEXT"
    INTERACTIVE = "INTERACTIVE"
    BUTTON = "BUTTON"
    UNSUPPORTED = "UNSUPPORTED"
