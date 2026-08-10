from app.bot import copy
from app.bot.handlers.offer import _relevant, offer_is_live
from tests.conftest import Harness

async def test_trace(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=copy.COURSE_UNSURE)
    for m in ("tell me about machine learning with agentic ai",
              "what does it cover", "okay how do i join"):
        r = harness.texts(await harness.say(m))
        st = await harness.state()
        async with harness.database.session() as s:
            from app.repositories.user_repository import UserRepository
            from app.repositories.conversation_repository import ConversationRepository
            u = await UserRepository(s).get_by_phone(harness.phone)
            c = await ConversationRepository(s).get_active(u.id)
            course = c.current_course if c else None
        print(f"{m[:40]!r:44} state={st:16} course={course} coupon={'BOT32' in r}")
