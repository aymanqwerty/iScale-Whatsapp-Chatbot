"""Knowledge loading, retrieval and prompt grounding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import KnowledgeBaseError
from app.domain.enums import ConversationState
from app.services.knowledge.consistency import check_business_hours
from app.services.knowledge.loader import KnowledgeBase, KnowledgeLoader
from app.services.knowledge.retriever import (
    KeywordRetriever,
    _singular,
    build_retriever,
    tokenize,
)
from app.services.llm.prompts import build_system_prompt, build_user_prompt


@pytest.fixture
def retriever(knowledge_base: KnowledgeBase) -> KeywordRetriever:
    return KeywordRetriever(knowledge_base, default_limit=4, default_max_chars=6000)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def test_courses_are_split_into_field_level_snippets(
    knowledge_base: KnowledgeBase,
) -> None:
    """A duration question must not have to drag the whole syllabus along."""
    snippets = knowledge_base.snippets_for_course("master-of-data-analytics")
    ids = {snippet.id for snippet in snippets}

    assert "course:master-of-data-analytics:overview" in ids
    assert "course:master-of-data-analytics:duration" in ids
    assert "course:master-of-data-analytics:fees" in ids
    assert "course:master-of-data-analytics:curriculum" in ids


def test_featured_and_other_courses_are_split(knowledge_base: KnowledgeBase) -> None:
    """The menu shows the flagged courses; the rest stay answerable behind them."""
    featured = {course.slug for course in knowledge_base.featured_courses}
    others = {course.slug for course in knowledge_base.other_courses}

    assert "ai-engineer-advance-program" in featured
    assert "free-data-analytics" in others
    assert not featured & others
    assert featured | others == {course.slug for course in knowledge_base.courses}


def test_grouped_faqs_and_policies_are_loaded(knowledge_base: KnowledgeBase) -> None:
    """Regression: a restructured file used to load as zero snippets, silently.

    `faqs.json` is keyed by category and `policies.json` by section; neither has
    the flat list the loader originally required.
    """
    by_source: dict[str, int] = {}
    for snippet in knowledge_base.snippets:
        by_source[snippet.source] = by_source.get(snippet.source, 0) + 1

    assert by_source.get("faq", 0) > 0
    assert by_source.get("policy", 0) > 0


def test_flat_faq_and_policy_layouts_still_load(tmp_path: Path) -> None:
    """The pre-restructure layout must keep working."""
    (tmp_path / "faqs.json").write_text(
        json.dumps({"faqs": [{"id": "faq-x", "question": "Q?", "answer": "A."}]}),
        encoding="utf-8",
    )
    (tmp_path / "policies.json").write_text(
        json.dumps({"policies": [{"id": "policy-x", "title": "T", "content": "C."}]}),
        encoding="utf-8",
    )

    knowledge_base = KnowledgeLoader(tmp_path).load()
    ids = {snippet.id for snippet in knowledge_base.snippets}

    assert "faq-x" in ids
    assert "policy-x" in ids


def test_missing_files_are_tolerated(tmp_path: Path) -> None:
    """A partial knowledge directory degrades; it does not crash the app."""
    (tmp_path / "company.json").write_text(
        json.dumps({"company": {"name": "iScale"}}), encoding="utf-8"
    )

    knowledge_base = KnowledgeLoader(tmp_path).load()

    assert knowledge_base.company_name == "iScale"
    assert knowledge_base.courses == []


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeBaseError):
        KnowledgeLoader(tmp_path / "nope").load()


def test_invalid_json_raises_with_the_filename(tmp_path: Path) -> None:
    (tmp_path / "courses.json").write_text("{ broken", encoding="utf-8")

    with pytest.raises(KnowledgeBaseError) as exc_info:
        KnowledgeLoader(tmp_path).load()

    assert "courses.json" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def test_bigrams_are_indexed(  ) -> None:
    assert "power bi" in tokenize("I want the power bi course")
    # Stopwords are dropped before bigrams are formed.
    assert "want the" not in tokenize("I want the power bi course")


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("what are the fees", "faq:fees:0"),
        ("can I pay in EMI", "faq:fees:0"),
        ("how can I enroll", "faq:admission:0"),
        ("are classes live", "faq:classes:0"),
        ("what salary can I expect", "placements:salary"),
        ("what is the refund policy", "policy:refund_policy"),
        ("what are your working hours", "policy:working_hours"),
    ],
)
def test_relevant_snippet_ranks_first(
    retriever: KeywordRetriever, query: str, expected_id: str
) -> None:
    hits = retriever.retrieve(query)

    assert hits
    assert hits[0].id == expected_id


@pytest.mark.parametrize(
    ("singular", "plural"),
    [
        ("what is the fee", "what are the fees"),
        ("tell me about the project", "what are the projects"),
        ("how many batches", "what batch is available"),
        ("what tool is covered", "what tools are covered"),
    ],
)
def test_singular_and_plural_retrieve_the_same_thing(
    retriever: KeywordRetriever, singular: str, plural: str
) -> None:
    """Regression: "fee" and "fees" were unrelated terms to BM25.

    In production the bot answered "I don't have information about the fee" for
    a course whose fees are in the knowledge base - the single most-asked
    question in the funnel - because the snippet is titled "fees" and nothing
    folded the two forms together.
    """
    course = "ai-engineer-advance-program"
    singular_hit = retriever.retrieve(singular, course=course)[0]
    plural_hit = retriever.retrieve(plural, course=course)[0]

    assert singular_hit.id == plural_hit.id
    assert singular_hit.course == course


def test_stemming_leaves_non_plurals_alone() -> None:
    """Words ending in "ss" or too short to be plural must not be truncated."""
    assert _singular("class") == "class"
    assert _singular("business") == "business"
    assert _singular("fee") == "fee"
    assert _singular("sms") == "sms"
    # Both sides of the index pass through this, so consistency is what matters.
    assert _singular("fees") == _singular("fee")
    assert _singular("batches") == _singular("batch")
    assert _singular("queries") == _singular("query")


def test_course_scope_prefers_the_selected_course(
    retriever: KeywordRetriever,
) -> None:
    hits = retriever.retrieve("how long is it", course="data-science-with-generative-ai")

    assert hits[0].id == "course:data-science-with-generative-ai:duration"
    # Another course's duration must not crowd in.
    assert not any(hit.id.startswith("course:master-of-data-analytics") for hit in hits)


def test_generic_question_prefers_general_sources(
    retriever: KeywordRetriever,
) -> None:
    """With no course chosen, one arbitrary course's fees is the wrong answer.

    Every course carries a near-identical fees snippet, so before the user has
    picked one the general FAQ is the only honest response.
    """
    hits = retriever.retrieve("what are the fees")

    assert hits[0].source in ("faq", "policy")


def test_naming_a_course_still_beats_the_general_answer(
    retriever: KeywordRetriever,
) -> None:
    """The damping must be mild enough that an explicit course still wins."""
    hits = retriever.retrieve("what is the fee for the AI Engineer Advance Program")

    assert hits[0].id.startswith("course:ai-engineer-advance-program")


def test_audience_filter_hides_post_sales_content(tmp_path: Path) -> None:
    """Enrolled-student content must never surface to a prospect, or vice versa.

    Built on a purpose-made knowledge directory rather than the shipped one: the
    real `faqs.json` is business-owned and currently tags nothing by audience,
    which would let this pass without exercising the filter at all.
    """
    (tmp_path / "faqs.json").write_text(
        json.dumps(
            {
                "faqs": [
                    {
                        "id": "faq-portal",
                        "question": "Where do I submit my assignment?",
                        "answer": "Through the student portal.",
                        "audience": "post_sales",
                    },
                    {
                        "id": "faq-demo",
                        "question": "Can I attend a demo assignment session?",
                        "answer": "Yes, book a demo class before joining.",
                        "audience": "pre_sales",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    retriever = KeywordRetriever(KnowledgeLoader(tmp_path).load())

    pre_sales = {hit.id for hit in retriever.retrieve("assignment", audience="pre_sales")}
    post_sales = {hit.id for hit in retriever.retrieve("assignment", audience="post_sales")}

    assert pre_sales == {"faq-demo"}
    assert post_sales == {"faq-portal"}


def test_retrieval_respects_the_snippet_limit(knowledge_base: KnowledgeBase) -> None:
    small = KeywordRetriever(knowledge_base, default_limit=2, default_max_chars=6000)

    assert len(small.retrieve("course fees duration placement")) <= 2


def test_retrieval_respects_the_character_budget(
    knowledge_base: KnowledgeBase,
) -> None:
    tiny = KeywordRetriever(knowledge_base, default_limit=10, default_max_chars=200)

    hits = tiny.retrieve("tell me everything about the data science curriculum")

    assert len(hits) >= 1
    # The first hit may exceed the budget on its own; the rest must not pile on.
    assert sum(len(hit) for hit in hits[1:]) < 200


def test_unmatchable_query_falls_back_to_context(
    retriever: KeywordRetriever,
) -> None:
    """Better to ground on company basics than to hand the model nothing."""
    hits = retriever.retrieve("qwertyuiop zxcvbnm", course="advance-python-with-ai-tools")

    assert hits
    assert hits[0].id == "course:advance-python-with-ai-tools:overview"


def test_retrieval_never_returns_the_whole_knowledge_base(
    knowledge_base: KnowledgeBase,
) -> None:
    full = build_retriever(knowledge_base, limit=6, max_chars=6000)

    hits = full.retrieve("course")

    assert len(hits) <= 6
    assert len(hits) < len(knowledge_base.snippets)


# --------------------------------------------------------------------------- #
# Prompt grounding
# --------------------------------------------------------------------------- #
def test_system_prompt_forbids_invention() -> None:
    prompt = build_system_prompt(
        company="iScale", state=ConversationState.COURSE_QNA, course_name="Data Science"
    )

    assert "ONLY using the KNOWLEDGE" in prompt
    assert "Data Science" in prompt
    # Discounts and coupons stay forbidden - those are sent by the state machine
    # with exact figures, and an improvised one is a price we must then honour.
    assert "coupon codes or special offers" in prompt
    # Fees are the deliberate exception: a published price in the KNOWLEDGE
    # section must be quoted, not deflected to a counselor. Refusing to answer
    # "what is the fees?" turned away the people closest to enrolling.
    assert "Fees ARE quotable" in prompt


def test_house_rules_reach_the_prompt(knowledge_base: KnowledgeBase) -> None:
    """`chatbot_rules.json` and `prompts.json` steer every answer."""
    prompt = build_system_prompt(
        company=knowledge_base.company_name,
        state=ConversationState.COURSE_QNA,
        rules=knowledge_base.rules,
        prompt_overrides=knowledge_base.prompt_overrides,
    )

    assert "HOUSE RULES" in prompt
    assert "Never guarantee placement." in prompt  # from never_do
    assert "Never invent facts." in prompt  # from prompts.json reminders


def test_prompt_forbids_claiming_to_have_booked_anything() -> None:
    """The model cannot write to the database, so it must never say it has.

    Observed in production: asked for a callback, the bot replied "your call is
    scheduled for 4 pm on Saturday" while no lead row existed. The person would
    have waited for a call nobody was ever told to make.
    """
    prompt = build_system_prompt(
        company="iScale", state=ConversationState.GENERAL_QNA
    )

    assert "NEVER CLAIM TO HAVE DONE SOMETHING" in prompt
    assert "I've scheduled" in prompt
    assert "cannot book calls" in prompt


def test_house_rules_are_optional() -> None:
    """A missing or empty rules file must not put a stray header in the prompt."""
    prompt = build_system_prompt(
        company="iScale", state=ConversationState.COURSE_QNA, rules={}
    )

    assert "HOUSE RULES" not in prompt
    # The hardcoded safety floor survives regardless.
    assert "ONLY using the KNOWLEDGE" in prompt


def test_business_hours_disagreement_is_reported() -> None:
    """The JSON files describe the hours; Settings enforces them.

    They are separate copies, so drift is silent - the bot would tell a user one
    window and then reject a slot inside it.
    """
    knowledge_base = KnowledgeBase(
        courses=[],
        snippets=[],
        company={},
        placements={},
        rules={"callback": {"business_hours": {"start": "09:00", "end": "21:00"}}},
        documents={"callback_rules.json": {"weekly_off": ["Sunday"]}},
    )

    warnings = check_business_hours(
        knowledge_base,
        Settings(_env_file=None, business_open_time="11:00", business_close_time="19:00"),
    )

    assert any("09:00" in w and "11:00" in w for w in warnings)
    assert any("21:00" in w and "19:00" in w for w in warnings)
    assert any("closes on" in w for w in warnings)


def test_matching_business_hours_are_silent(knowledge_base: KnowledgeBase) -> None:
    """The shipped files agree with the shipped defaults; no noise at startup."""
    assert check_business_hours(knowledge_base, Settings(_env_file=None)) == []


def test_support_state_adds_support_instructions() -> None:
    prompt = build_system_prompt(
        company="iScale", state=ConversationState.SUPPORT_QUERY
    )

    assert "SUPPORT MODE" in prompt


def test_empty_knowledge_block_says_so_explicitly() -> None:
    """An empty section reads as "unconstrained" to a model; this must not."""
    prompt = build_user_prompt("what are the fees?", [])

    assert "No relevant information was found" in prompt
    assert "Do not attempt to answer it from your own knowledge" in prompt


def test_user_prompt_carries_the_retrieved_snippets(
    retriever: KeywordRetriever,
) -> None:
    snippets = retriever.retrieve("what are the fees", course="master-of-data-analytics")

    prompt = build_user_prompt("what are the fees", snippets)

    assert "KNOWLEDGE" in prompt
    assert "what are the fees" in prompt
    assert any(snippet.title in prompt for snippet in snippets)
