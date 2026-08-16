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
from app.core.events import broadcaster
from app.core.logging import get_logger
from app.db.session import Database
from app.db.models.message import Message
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


#: Ceiling on a stored attachment. A payment screenshot is a few hundred KB;
#: WhatsApp itself caps images at 5 MB, so this refuses only the pathological.
MAX_MEDIA_BYTES = 5 * 1024 * 1024


def _transcript_text(inbound: InboundMessage) -> str:
    """What the console should show for this message.

    Media has no text, and storing the empty string left a blank row in the
    transcript - so an agent asked to verify a payment saw nothing at all where
    the screenshot should be. A placeholder is not the image, but it does say
    that something arrived and what kind of thing it was.
    """
    if inbound.text or inbound.reply_id:
        return inbound.text or (inbound.reply_id or "")
    if inbound.media_type:
        # No "open WhatsApp to view" - there is no WhatsApp on our side. Cloud
        # API is an API, so the console shows the picture itself and this line
        # is only the fallback for one it could not fetch.
        return f"[{inbound.media_type}]"
    return ""


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

            # Block gate. FIRST, and ahead of even storing the message, which is
            # the difference between a block and a handover: a paused
            # conversation is still recorded because a human is about to answer
            # it, whereas a blocked one is meant to stop existing for us. So
            # nothing is written, nothing is answered, and the console is not
            # woken - a blocked spammer cannot push a real customer down the
            # inbox or grow the transcript table unboundedly.
            #
            # The history from before the block is untouched, so an agent can
            # still read why they blocked the number and undo it.
            if user.blocked:
                logger.info(
                    "Message from a blocked contact ignored",
                    extra={"wa_message_id": inbound.wa_message_id},
                )
                return TurnResult(), user.phone

            conversation = await conversations.get_or_create_active(
                user.id, for_update=True
            )

            stored = await messages.add(
                conversation_id=conversation.id,
                sender=MessageSender.USER,
                message=_transcript_text(inbound),
                wa_message_id=inbound.wa_message_id,
                state=conversation.current_state,
                # WhatsApp's own send time, so a redelivered message is
                # recognisable as old rather than looking freshly sent.
                timestamp=inbound.timestamp,
            )

            # Handover gate. Placed AFTER the message is stored and before any
            # handler runs, which is exactly the behaviour a human takeover
            # needs: the console still sees the customer's messages arriving
            # live, and the transcript stays complete for when the bot is handed
            # back - but nothing is generated, so the customer never gets a menu
            # or a sales nudge in the middle of talking to a person.
            if user.bot_paused:
                await conversations.touch(conversation, last_message=inbound.text)
                # The console is the only thing that will answer now, so it has
                # to know the moment this lands.
                broadcaster.publish(user.phone)
                logger.info(
                    "Message recorded but not answered - conversation is with an agent",
                    extra={"wa_message_id": inbound.wa_message_id},
                )
                return TurnResult(), user.phone

            history = await self._load_history(messages, conversation.id)
            ctx = TurnContext(
                inbound=inbound,
                user=user,
                conversation=conversation,
                deps=self._build_dependencies(session),
                history=history,
            )

            result = await self._machine.handle(ctx)

            # After the machine, because the machine is what decides an image is
            # a payment proof. Only those are stored: that bounds this to people
            # who reached the checkout step, rather than letting anyone fill the
            # database with pictures.
            if user.payment_proof_at is not None and inbound.media_id:
                await self._store_media(stored, inbound)

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

            broadcaster.publish(user.phone)
            return result, user.phone

    # ------------------------------------------------------------------ #
    async def _store_media(self, record: Message, inbound: InboundMessage) -> None:
        """Pull the attachment down from Meta and keep it.

        Best effort throughout. A screenshot we cannot fetch must not fail the
        turn that received it: the customer has just paid us, and the worst
        possible response to that is an error. They still get their
        acknowledgement and the console still shows a payment arrived - only the
        picture is missing, and an agent can ask for it again.
        """
        assert inbound.media_id is not None
        try:
            fetched = await self._messaging.download_media(inbound.media_id)
        except Exception:
            logger.exception("Media download raised", extra={"media": inbound.media_id})
            return
        if fetched is None:
            return

        data, mime = fetched
        if len(data) > MAX_MEDIA_BYTES:
            # A payment screenshot is a few hundred KB. Anything far larger is
            # not one, and is not worth carrying in every row read.
            logger.warning(
                "Attachment too large to store",
                extra={"bytes": len(data), "limit": MAX_MEDIA_BYTES},
            )
            return

        record.media_data = data
        record.media_mime = inbound.media_mime or mime
        logger.info("Payment screenshot stored", extra={"bytes": len(data)})

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
