"""Orchestration for one inbound message.

Responsibilities, in order: de-duplicate, load or create the user and
conversation, persist the transcript, run the state machine, commit, then send
the replies.

Committing before sending is deliberate. The webhook has already returned 200 to
Meta, so a rollback would not be retried by anyone - persisting the advanced
state first means a WhatsApp outage costs the user a reply, not the whole
conversation. Send failures are logged and the client retries internally.
"""

from __future__ import annotations

from app.bot.context import BotDependencies, TurnContext
from app.bot.machine import ConversationMachine
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.domain.enums import MessageSender
from app.domain.messaging import InboundMessage, OutboundMessage, TurnResult
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.knowledge.loader import KnowledgeBase
from app.services.lead_service import LeadService, LeadSyncService
from app.services.llm.answer_service import AnswerService
from app.services.llm.base import ChatTurn
from app.services.scheduling.callback_time import CallbackTimeValidator
from app.services.whatsapp.base import MessagingClient

logger = get_logger(__name__)


class ConversationService:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        knowledge_base: KnowledgeBase,
        answer_service: AnswerService,
        callback_validator: CallbackTimeValidator,
        messaging: MessagingClient,
        lead_sync: LeadSyncService,
        machine: ConversationMachine | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._knowledge_base = knowledge_base
        self._answer_service = answer_service
        self._callback_validator = callback_validator
        self._messaging = messaging
        self._lead_sync = lead_sync
        self._machine = machine or ConversationMachine()

    # ------------------------------------------------------------------ #
    async def process_inbound(self, inbound: InboundMessage) -> TurnResult:
        """Handle one message end to end. Never raises."""
        try:
            result, phone = await self._run_turn(inbound)
        except Exception:
            logger.exception(
                "Turn failed", extra={"wa_message_id": inbound.wa_message_id}
            )
            await self._send_error(inbound.from_phone)
            return TurnResult()

        await self._deliver(phone, result.replies)

        if result.lead_id is not None:
            # Out of band: a slow spreadsheet must not delay the confirmation.
            await self._lead_sync.sync(result.lead_id)

        return result

    # ------------------------------------------------------------------ #
    async def _run_turn(self, inbound: InboundMessage) -> tuple[TurnResult, str]:
        async with self._database.session() as session:
            messages = MessageRepository(session)

            if await messages.exists_wa_message(inbound.wa_message_id):
                logger.info(
                    "Duplicate webhook delivery ignored",
                    extra={"wa_message_id": inbound.wa_message_id},
                )
                return TurnResult(), inbound.from_phone

            users = UserRepository(session)
            conversations = ConversationRepository(session)

            user = await users.get_or_create(
                inbound.from_phone, profile_name=inbound.profile_name
            )
            conversation = await conversations.get_or_create_active(
                user.id, for_update=True
            )

            await messages.add(
                conversation_id=conversation.id,
                sender=MessageSender.USER,
                message=inbound.text or (inbound.reply_id or ""),
                wa_message_id=inbound.wa_message_id,
                state=conversation.current_state,
            )

            history = await self._load_history(messages, conversation.id)
            ctx = TurnContext(
                inbound=inbound,
                user=user,
                conversation=conversation,
                deps=self._build_dependencies(session),
                history=history,
            )

            result = await self._machine.handle(ctx)

            for reply in result.replies:
                await messages.add(
                    conversation_id=conversation.id,
                    sender=MessageSender.BOT,
                    message=reply.text,
                    state=conversation.current_state,
                )

            await conversations.touch(conversation, last_message=inbound.text)

            if result.close_conversation:
                await conversations.close(conversation)

            return result, user.phone

    # ------------------------------------------------------------------ #
    def _build_dependencies(self, session: object) -> BotDependencies:
        return BotDependencies(
            settings=self._settings,
            knowledge_base=self._knowledge_base,
            answer_service=self._answer_service,
            callback_validator=self._callback_validator,
            lead_service=LeadService(session, self._settings),  # type: ignore[arg-type]
            lead_repository=LeadRepository(session),  # type: ignore[arg-type]
        )

    async def _load_history(
        self, messages: MessageRepository, conversation_id: int
    ) -> tuple[ChatTurn, ...]:
        """Recent transcript as model turns.

        The message just received is dropped - it is passed separately as the
        current question, and including it twice makes the model echo itself.
        """
        limit = self._settings.history_message_limit
        recent = await messages.recent(conversation_id, limit=limit + 1)
        turns = [
            ChatTurn(
                role="user" if record.sender is MessageSender.USER else "assistant",
                content=record.message,
            )
            for record in recent[:-1]
            if record.message.strip()
        ]
        return tuple(turns[-limit:])

    # ------------------------------------------------------------------ #
    async def _deliver(self, phone: str, replies: list[OutboundMessage]) -> None:
        for reply in replies:
            try:
                await self._messaging.send(phone, reply)
            except Exception:
                logger.exception("Failed to deliver reply", extra={"phone": phone[:4]})

    async def _send_error(self, phone: str) -> None:
        from app.bot import copy

        try:
            await self._messaging.send(phone, OutboundMessage(text=copy.ERROR_MESSAGE))
        except Exception:
            logger.exception("Could not deliver error message")
