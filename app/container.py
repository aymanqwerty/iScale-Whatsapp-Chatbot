"""Composition root.

Every concrete implementation is chosen here, once, at startup. Nothing below
this module imports a concrete collaborator - they receive protocols. Swapping
Google Sheets for HubSpot, or the keyword retriever for a vector store, is an
edit to this file alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.services.conversation_service import ConversationService
from app.services.crm.base import LeadSink
from app.services.crm.google_sheets import GoogleSheetsLeadSink
from app.services.crm.null_sink import NullLeadSink
from app.services.knowledge.loader import KnowledgeBase, load_knowledge_base
from app.services.knowledge.retriever import KnowledgeRetriever, build_retriever
from app.services.lead_service import LeadSyncService
from app.services.llm.answer_service import AnswerService
from app.services.llm.base import LLMClient
from app.services.llm.groq import GroqClient
from app.services.scheduling.callback_time import CallbackTimeValidator
from app.services.whatsapp.allowlist import PhoneAllowlist
from app.services.whatsapp.base import MessagingClient
from app.services.whatsapp.client import WhatsAppClient
from app.services.whatsapp.guarded_client import GuardedMessagingClient
from app.services.whatsapp.logging_client import LoggingMessagingClient

logger = get_logger(__name__)


@dataclass(slots=True)
class Container:
    """Process-wide singletons, built once and shared by every request."""

    settings: Settings
    database: Database
    knowledge_base: KnowledgeBase
    retriever: KnowledgeRetriever
    llm: LLMClient
    answer_service: AnswerService
    callback_validator: CallbackTimeValidator
    messaging: MessagingClient
    allowlist: PhoneAllowlist
    lead_sink: LeadSink
    lead_sync: LeadSyncService

    # ------------------------------------------------------------------ #
    @classmethod
    def build(cls, settings: Settings) -> Container:
        database = Database(settings)

        knowledge_base = load_knowledge_base(settings.knowledge_dir)
        retriever = build_retriever(
            knowledge_base,
            limit=settings.knowledge_max_snippets,
            max_chars=settings.knowledge_max_chars,
        )

        llm: LLMClient = GroqClient(settings)
        answer_service = AnswerService(
            llm=llm, retriever=retriever, knowledge_base=knowledge_base
        )

        messaging: MessagingClient = (
            WhatsAppClient(settings)
            if settings.whatsapp_enabled
            else LoggingMessagingClient()
        )
        if not settings.whatsapp_enabled:
            logger.warning(
                "WHATSAPP_ENABLED is false - replies will be logged, not sent"
            )

        # The outbound half of the development guard. Wrapping here means every
        # send in the application goes through it, whatever the call path.
        allowlist = PhoneAllowlist(
            enabled=settings.whatsapp_allowlist_enabled,
            numbers=settings.whatsapp_allowed_numbers,
        )
        allowlist.log_startup_banner()
        if allowlist.enabled:
            messaging = GuardedMessagingClient(messaging, allowlist)

        lead_sink: LeadSink = (
            GoogleSheetsLeadSink(settings)
            if settings.google_sheets_enabled
            else NullLeadSink()
        )
        if not settings.google_sheets_enabled:
            logger.info("Google Sheets sync disabled - leads are stored in PostgreSQL only")

        return cls(
            settings=settings,
            database=database,
            knowledge_base=knowledge_base,
            retriever=retriever,
            llm=llm,
            answer_service=answer_service,
            callback_validator=CallbackTimeValidator(settings),
            messaging=messaging,
            allowlist=allowlist,
            lead_sink=lead_sink,
            lead_sync=LeadSyncService(database, lead_sink, settings),
        )

    # ------------------------------------------------------------------ #
    def conversation_service(self) -> ConversationService:
        """Build the per-message orchestrator (cheap; holds no state)."""
        return ConversationService(
            database=self.database,
            settings=self.settings,
            knowledge_base=self.knowledge_base,
            answer_service=self.answer_service,
            callback_validator=self.callback_validator,
            messaging=self.messaging,
            lead_sync=self.lead_sync,
        )

    async def shutdown(self) -> None:
        await self.messaging.close()
        await self.database.dispose()
