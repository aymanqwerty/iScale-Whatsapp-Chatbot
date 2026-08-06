"""Knowledge loading, retrieval and prompt grounding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.exceptions import KnowledgeBaseError
from app.domain.enums import ConversationState
from app.services.knowledge.loader import KnowledgeBase, KnowledgeLoader
from app.services.knowledge.retriever import KeywordRetriever, build_retriever, tokenize
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
    snippets = knowledge_base.snippets_for_course("data-analytics")
    ids = {snippet.id for snippet in snippets}

    assert "course:data-analytics:overview" in ids
    assert "course:data-analytics:duration" in ids
    assert "course:data-analytics:fees" in ids
    assert "course:data-analytics:curriculum" in ids


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
        ("what are the fees", "faq-fees"),
        ("do you have weekend batches", "faq-weekend"),
        ("what salary can I expect", "placements:salary"),
        ("what is the refund policy", "policy-refund"),
        ("difference between analytics and data science", "faq-analytics-vs-science"),
        ("what are your working hours", "company:contact"),
    ],
)
def test_relevant_snippet_ranks_first(
    retriever: KeywordRetriever, query: str, expected_id: str
) -> None:
    hits = retriever.retrieve(query)

    assert hits
    assert hits[0].id == expected_id


def test_course_scope_prefers_the_selected_course(
    retriever: KeywordRetriever,
) -> None:
    hits = retriever.retrieve("how long is it", course="data-science")

    assert hits[0].id == "course:data-science:duration"
    # Another course's duration must not crowd in.
    assert not any(hit.id.startswith("course:python") for hit in hits)


def test_audience_filter_hides_post_sales_content(
    retriever: KeywordRetriever,
) -> None:
    hits = retriever.retrieve("assignment submission", audience="pre_sales")

    assert all(hit.audience in ("all", "pre_sales") for hit in hits)
    assert not any(hit.id == "faq-assignment-submission" for hit in hits)


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
    hits = retriever.retrieve("qwertyuiop zxcvbnm", course="python")

    assert hits
    assert hits[0].id == "course:python:overview"


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
    assert "fees, discounts, scholarships" in prompt
    assert "Data Science" in prompt


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
    snippets = retriever.retrieve("what are the fees", course="data-analytics")

    prompt = build_user_prompt("what are the fees", snippets)

    assert "KNOWLEDGE" in prompt
    assert "what are the fees" in prompt
    assert any(snippet.title in prompt for snippet in snippets)
