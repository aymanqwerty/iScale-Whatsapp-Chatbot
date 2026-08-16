"""Shared fixtures.

The suite runs the real state machine, repositories and orchestrator against a
temporary SQLite database, with only the two network boundaries (Groq and
WhatsApp) replaced by fakes. That keeps the tests fast and hermetic while still
exercising the code that actually ships.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.bot import copy as _copy
from app.bot.machine import ConversationMachine
from app.core.config import Settings
from app.db.base import Base
from app.db.session import Database
from app.domain.enums import MessageKind
from app.domain.messaging import InboundMessage, OutboundMessage
from app.services.conversation_service import ConversationService
from app.services.crm.base import LeadRecord
from app.services.crm.null_sink import NullLeadSink
from app.services.knowledge.loader import KnowledgeBase, load_knowledge_base
from app.services.knowledge.retriever import build_retriever
from app.services.lead_service import LeadSyncService
from app.services.llm.answer_service import AnswerService
from app.services.llm.base import ChatTurn
from app.services.scheduling.callback_time import CallbackTimeValidator
from app.services.whatsapp.logging_client import LoggingMessagingClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@dataclass
class FakeLLM:
    """Records prompts and returns a scripted reply."""

    reply: str = "Here is a helpful answer about the program."
    fail: bool = False
    #: Exception raised when `fail` is set. Defaults to a transient model error;
    #: tests override it to cover misconfiguration and unexpected SDK failures.
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        history: list[ChatTurn] | None = None,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "history": list(history or []),
            }
        )
        if self.fail:
            from app.core.exceptions import LLMError

            raise self.error or LLMError("simulated model outage")
        return self.reply

    async def health_check(self) -> bool:
        return not self.fail


class FrozenClockValidator(CallbackTimeValidator):
    """Callback validator pinned to a fixed "now".

    Flow tests say things like "tomorrow 3pm". Against the real clock those
    tests would pass or fail depending on the day they run - "tomorrow" is a
    closed Friday once a week. Pinning the reference to a Wednesday makes the
    whole suite deterministic without weakening what it checks.
    """

    #: Wednesday 5 August 2026, 12:00 IST - mid-week, office open.
    FIXED_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    def now(self) -> datetime:
        return self.FIXED_NOW


class RecordingSink(NullLeadSink):
    """Null sink that reports itself as enabled, so sync paths are exercised."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self._fail = fail

    @property
    def name(self) -> str:
        return "recording"

    @property
    def enabled(self) -> bool:
        return True

    async def push_lead(self, record: LeadRecord) -> None:
        if self._fail:
            from app.core.exceptions import CRMError

            raise CRMError("simulated sheet failure")
        self.pushed.append(record)


# --------------------------------------------------------------------------- #
# Settings / database
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db_file = tmp_path / "test.db"
    return Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        knowledge_dir=PROJECT_ROOT / "knowledge",
        whatsapp_enabled=False,
        google_sheets_enabled=False,
        qna_nudge_threshold=3,
        business_timezone="Asia/Kolkata",
        log_level="WARNING",
    )


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings)
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield db
    await db.dispose()


@pytest.fixture(scope="session")
def knowledge_base() -> KnowledgeBase:
    return load_knowledge_base(PROJECT_ROOT / "knowledge")


# --------------------------------------------------------------------------- #
# Conversation harness
# --------------------------------------------------------------------------- #
@dataclass
class Harness:
    """Drives a conversation the way the webhook would."""

    service: ConversationService
    database: Database
    llm: FakeLLM
    messaging: LoggingMessagingClient
    sink: RecordingSink
    phone: str = "919876543210"

    async def say(
        self, text: str = "", *, reply_id: str | None = None, name: str | None = None
    ) -> list[OutboundMessage]:
        """Send one message and return the replies it produced."""
        before = len(self.messaging.sent)
        inbound = InboundMessage(
            wa_message_id=f"wamid.{uuid.uuid4().hex}",
            from_phone=self.phone,
            kind=MessageKind.INTERACTIVE if reply_id else MessageKind.TEXT,
            text=text,
            reply_id=reply_id,
            profile_name=name,
            timestamp=datetime.now(),
        )
        await self.service.process_inbound(inbound)
        return [message for _, message in self.messaging.sent[before:]]

    async def send_media(
        self,
        media_type: str = "image",
        *,
        data: bytes | None = None,
        mime: str = "image/jpeg",
    ) -> list[OutboundMessage]:
        """Send a payload we cannot read as text - a screenshot, say.

        `data` stubs what Cloud API would hand back for this attachment, so the
        download-and-store path can be exercised without a network call.
        """
        before = len(self.messaging.sent)
        media_id = f"media.{uuid.uuid4().hex}" if data is not None else None
        if data is not None and media_id is not None:
            self.messaging.media[media_id] = (data, mime)
        inbound = InboundMessage(
            wa_message_id=f"wamid.{uuid.uuid4().hex}",
            from_phone=self.phone,
            kind=MessageKind.UNSUPPORTED,
            media_type=media_type,
            media_id=media_id,
            media_mime=mime if data is not None else None,
            timestamp=datetime.now(),
        )
        await self.service.process_inbound(inbound)
        return [message for _, message in self.messaging.sent[before:]]

    async def give_name(self, name: str) -> list[OutboundMessage]:
        """Answer the name question, then confirm the WhatsApp number.

        Capture asks for the callback number between the name and the time, so
        almost every booking test needs both steps. Kept here rather than
        repeated inline so the next change to the slot order touches one place
        instead of every test that books a call.
        """
        replies = await self.say(name)
        # Post-sales asks for email and course before the number, so the phone
        # question may not be next. Confirm it only if that is what was asked.
        if await self.state() == "ASK_PHONE":
            replies = await self.say(reply_id=_copy.PHONE_CONFIRM)
        return replies

    async def state(self) -> str:
        from app.repositories.conversation_repository import ConversationRepository
        from app.repositories.user_repository import UserRepository

        async with self.database.session() as session:
            user = await UserRepository(session).get_by_phone(self.phone)
            if user is None:
                return "UNKNOWN"
            conversation = await ConversationRepository(session).get_active(user.id)
            return str(conversation.current_state) if conversation else "CLOSED"

    async def leads(self) -> list[object]:
        from app.repositories.lead_repository import LeadRepository

        async with self.database.session() as session:
            return list(await LeadRepository(session).list_recent())

    @staticmethod
    def texts(messages: list[OutboundMessage]) -> str:
        return "\n".join(message.text for message in messages)


@pytest_asyncio.fixture
async def harness(
    settings: Settings, database: Database, knowledge_base: KnowledgeBase
) -> Harness:
    llm = FakeLLM()
    messaging = LoggingMessagingClient()
    sink = RecordingSink()

    retriever = build_retriever(
        knowledge_base,
        limit=settings.knowledge_max_snippets,
        max_chars=settings.knowledge_max_chars,
    )
    answer_service = AnswerService(
        llm=llm, retriever=retriever, knowledge_base=knowledge_base
    )

    service = ConversationService(
        database=database,
        settings=settings,
        knowledge_base=knowledge_base,
        answer_service=answer_service,
        callback_validator=FrozenClockValidator(settings),
        messaging=messaging,
        lead_sync=LeadSyncService(database, sink, settings),
        machine=ConversationMachine(),
    )

    return Harness(
        service=service,
        database=database,
        llm=llm,
        messaging=messaging,
        sink=sink,
    )


# --------------------------------------------------------------------------- #
# API clients for the security tests
# --------------------------------------------------------------------------- #
def _client_with(settings_overrides: dict[str, object], tmp_path: Path):
    """A TestClient over a real app, with a throwaway SQLite database."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from app.main import create_app

    db_file = tmp_path / f"sec-{uuid.uuid4().hex}.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{db_file}",
        knowledge_dir=PROJECT_ROOT / "knowledge",
        whatsapp_enabled=False,
        google_sheets_enabled=False,
        log_level="WARNING",
        **settings_overrides,  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings))


@pytest.fixture
def api_key_client(tmp_path: Path) -> Iterator[Any]:
    """App configured with a known API key."""
    with _client_with({"environment": "test", "api_key": "s3cret-key"}, tmp_path) as c:
        yield c


@pytest.fixture
def unconfigured_prod_client(tmp_path: Path) -> Iterator[Any]:
    """Production with no API key set - lead data must fail closed."""
    with _client_with({"environment": "production", "api_key": ""}, tmp_path) as c:
        yield c
