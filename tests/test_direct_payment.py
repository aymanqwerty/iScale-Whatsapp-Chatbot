"""Direct payment: the Razorpay route that undercuts the coupon.

The point of this feature is to close the sale inside the chat rather than hand
it to a counselor, so these tests are mostly about the two ways that goes wrong:
promising a price the checkout page will not honour, and failing to recognise
the screenshot that arrives afterwards.
"""

from __future__ import annotations

import json

from app.bot import copy
from tests.conftest import PROJECT_ROOT, Harness


def _offer() -> dict[str, object]:
    path = PROJECT_ROOT / "knowledge" / "offers.json"
    return dict(json.loads(path.read_text(encoding="utf-8"))["offer"])


OFFER = _offer()
DIRECT = dict(OFFER.get("direct_payment") or {})


async def _reach_the_offer(harness: Harness) -> list:
    """Drive a real conversation to the point where the discount appears.

    The discovery branch, because that is where the funnel steers people toward
    the discounted course - and an explicit buying signal, which earns the offer
    immediately rather than after the engagement quota.
    """
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=copy.COURSE_UNSURE)
    return await harness.say("i am a doctor, how do i join this course")


# --------------------------------------------------------------------------- #
# The offer message
# --------------------------------------------------------------------------- #
async def test_the_offer_advertises_the_better_direct_price(harness: Harness) -> None:
    """The coupon is the fallback now; direct payment is the headline."""
    replies = await _reach_the_offer(harness)
    text = " ".join(r.text for r in replies).lower()

    # Asserted on meaning, not wording - this copy gets tuned often, and a test
    # that pins the exact sentence just breaks every time it improves.
    assert "payment link" in text, "the direct-payment route was never offered"
    assert any(
        word in text for word in ("cheaper", "biggest discount", "best price")
    ), "nothing said the direct route is better"
    assert str(OFFER["coupon_code"]).lower() in text, "the coupon vanished"


async def test_the_offer_never_quotes_a_direct_payment_figure(
    harness: Harness,
) -> None:
    """Razorpay owns that number.

    A price written here would be a second source of truth, and the first time
    the two disagree the bot is contradicted at the checkout page - losing the
    sale and the trust together.
    """
    replies = await _reach_the_offer(harness)
    text = " ".join(r.text for r in replies)

    # The coupon's own figures are fine - those come from offers.json and are
    # what the website charges. What must not appear is a *third* number.
    allowed = {
        str(OFFER["list_price_inr"]),
        str(OFFER["final_price_inr"]),
        f"{int(OFFER['list_price_inr']):,}",
        f"{int(OFFER['final_price_inr']):,}",
        str(OFFER["discount_percent"]),
    }
    after = text.split("lowest price")[-1] if "lowest price" in text else ""
    for token in after.replace("₹", " ").split():
        cleaned = token.strip(".,!*_-()")
        if cleaned.replace(",", "").isdigit() and cleaned not in allowed:
            raise AssertionError(f"a price was invented for direct payment: {cleaned}")


async def test_the_pay_button_sends_both_links(harness: Harness) -> None:
    """Both, labelled - never one picked on the customer's behalf.

    Guessing the country from the dialling code is right most of the time, and
    wrong exactly when it costs a failed payment at the moment someone decided
    to buy.
    """
    await _reach_the_offer(harness)
    replies = await harness.say(reply_id=copy.OFFER_PAY_NOW)
    text = " ".join(r.text for r in replies)

    assert str(DIRECT["india_url"]) in text, "the Indian link is missing"
    assert str(DIRECT["international_url"]) in text, "the international link is missing"
    assert "india" in text.lower()


async def test_the_pay_message_asks_for_a_screenshot(harness: Harness) -> None:
    """Nothing here can see a Razorpay payment, so the proof has to be asked for."""
    await _reach_the_offer(harness)
    replies = await harness.say(reply_id=copy.OFFER_PAY_NOW)

    assert "screenshot" in " ".join(r.text for r in replies).lower()


async def test_a_counselor_is_still_offered(harness: Harness) -> None:
    """A discount is not a reason to take the counselor away.

    Plenty of people will not put a card into a link a chatbot sent them, and
    losing those is worse than the margin saved.
    """
    replies = await _reach_the_offer(harness)
    ids = {oid for r in replies for oid, _ in r.options}

    assert copy.MENU_COUNSELOR in ids


# --------------------------------------------------------------------------- #
# The screenshot
# --------------------------------------------------------------------------- #
async def test_a_screenshot_after_the_link_is_treated_as_payment(
    harness: Harness,
) -> None:
    """The reply that used to be sent here was "I can only read text messages".

    Immediately after asking for a screenshot, that reads as broken at the one
    moment the customer has actually paid.
    """
    await _reach_the_offer(harness)
    await harness.say(reply_id=copy.OFFER_PAY_NOW)

    replies = await harness.send_media("image")
    text = " ".join(r.text for r in replies).lower()

    assert "verify" in text or "team" in text
    assert "only read text" not in text


async def test_the_screenshot_does_not_confirm_the_payment(harness: Harness) -> None:
    """We cannot see the money.

    The image could be a failed attempt or the wrong screenshot entirely, so
    "your seat is confirmed" would be a promise made by something blind to it.
    """
    await _reach_the_offer(harness)
    await harness.say(reply_id=copy.OFFER_PAY_NOW)

    replies = await harness.send_media("image")
    text = " ".join(r.text for r in replies).lower()

    assert "confirmed" not in text.replace("confirm your seat", "")


async def test_the_screenshot_raises_a_flag_for_the_team(harness: Harness) -> None:
    from app.repositories.user_repository import UserRepository

    await _reach_the_offer(harness)
    await harness.say(reply_id=copy.OFFER_PAY_NOW)
    await harness.send_media("image")

    async with harness.database.session() as session:
        user = await UserRepository(session).get_by_phone(harness.phone)
        assert user is not None
        assert user.payment_proof_at is not None, "the team would never see it"


async def test_the_screenshot_itself_is_stored(harness: Harness) -> None:
    """There is no WhatsApp app on our side to open the picture in.

    Cloud API keeps media on Meta's servers behind the access token and expires
    it, so a screenshot that is not pulled down and stored is simply lost - and
    the team is asked to verify a payment they cannot see.
    """
    from sqlalchemy import desc, select

    from app.db.models.message import Message

    await _reach_the_offer(harness)
    await harness.say(reply_id=copy.OFFER_PAY_NOW)
    await harness.send_media("image", data=b"\xff\xd8\xff-fake-jpeg", mime="image/jpeg")

    async with harness.database.session() as session:
        row = (
            await session.execute(
                select(Message)
                .where(Message.media_data.is_not(None))
                .order_by(desc(Message.id))
                .limit(1)
            )
        ).scalar_one_or_none()

    assert row is not None, "the screenshot was never stored"
    assert row.media_data == b"\xff\xd8\xff-fake-jpeg"
    assert row.media_mime == "image/jpeg"


async def test_an_image_we_cannot_fetch_still_acknowledges(harness: Harness) -> None:
    """A failed download must not fail the turn.

    The customer has just paid us; the worst possible response is an error.
    """
    await _reach_the_offer(harness)
    await harness.say(reply_id=copy.OFFER_PAY_NOW)

    # No stubbed bytes, so download_media returns None.
    replies = await harness.send_media("image")
    text = " ".join(r.text for r in replies).lower()

    assert "team" in text, "the acknowledgement was lost with the download"


async def test_an_ordinary_image_is_not_stored(harness: Harness) -> None:
    """Only payment proofs. Otherwise anyone can fill the database with pictures."""
    from sqlalchemy import select

    from app.db.models.message import Message

    await harness.say("hi")
    await harness.send_media("image", data=b"not-a-payment", mime="image/png")

    async with harness.database.session() as session:
        stored = (
            await session.execute(select(Message).where(Message.media_data.is_not(None)))
        ).scalars().all()

    assert not stored, "an unsolicited image was stored"


async def test_the_screenshot_is_visible_in_the_transcript(harness: Harness) -> None:
    """An empty row is no use to an agent asked to verify a payment."""
    from sqlalchemy import desc, select

    from app.db.models.message import Message

    await _reach_the_offer(harness)
    await harness.say(reply_id=copy.OFFER_PAY_NOW)
    await harness.send_media("image")

    async with harness.database.session() as session:
        rows = (
            await session.execute(select(Message).order_by(desc(Message.id)).limit(6))
        ).scalars().all()

    assert any("image" in (m.message or "") for m in rows), "the image left no trace"


async def test_an_image_before_any_payment_link_is_not_a_payment(
    harness: Harness,
) -> None:
    """Someone sending a picture mid-conversation has not paid us."""
    from app.repositories.user_repository import UserRepository

    await harness.say("hi")
    replies = await harness.send_media("image")

    async with harness.database.session() as session:
        user = await UserRepository(session).get_by_phone(harness.phone)
        assert user is not None
        assert user.payment_proof_at is None, "a stray image was booked as a payment"

    # ...but it must not get the flat brush-off either, in case it IS a payment
    # arranged some other way.
    text = " ".join(r.text for r in replies).lower()
    assert "payment" in text


async def test_a_voice_note_still_gets_the_text_only_reply(harness: Harness) -> None:
    """Only images are payment-shaped. Audio is someone who cannot type."""
    await _reach_the_offer(harness)
    await harness.say(reply_id=copy.OFFER_PAY_NOW)

    replies = await harness.send_media("audio")
    text = " ".join(r.text for r in replies).lower()

    assert "only read text" in text


async def test_the_flow_still_works_with_direct_payment_off(harness: Harness) -> None:
    """`enabled: false` must fall back to the coupon, not to a broken message."""
    offer = dict(harness.service._knowledge_base.offer)
    offer["direct_payment"] = {"enabled": False}
    harness.service._knowledge_base._offer = offer  # type: ignore[attr-defined]

    replies = await _reach_the_offer(harness)
    text = " ".join(r.text for r in replies)
    ids = {oid for r in replies for oid, _ in r.options}

    assert str(OFFER["coupon_code"]) in text
    assert copy.OFFER_DONE in ids, "the original enrol button did not come back"
    assert copy.OFFER_PAY_NOW not in ids
