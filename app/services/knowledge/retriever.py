"""Relevance retrieval over the knowledge base.

`KnowledgeRetriever` is the seam for future upgrades: swapping this keyword
implementation for embeddings + a vector database means writing a new class that
satisfies the same protocol. Nothing above it changes.

The default implementation is BM25 over a small in-memory corpus. For a few
hundred snippets that is instant, needs no extra infrastructure, and - unlike a
naive substring match - correctly prefers "what does the course cost" -> the fees
snippet rather than every snippet containing the word "course".
"""

from __future__ import annotations

import math
import re
from collections import Counter
from itertools import pairwise
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger
from app.services.knowledge.loader import KnowledgeBase
from app.services.knowledge.models import Audience, KnowledgeSnippet

logger = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9+#]+")

_STOPWORDS = frozenset(
    """
    a an the is are am was were be been being do does did doing have has had having
    i me my we our you your he she it they them this that these those what which who
    whom when where why how can could should would will shall may might must
    of in on at to for with about from by as into over after before and or but if
    not no yes so than then there here also very just only more most much many
    want need know tell give get please thanks thank hi hello hey ok okay
    """.split()
)

# BM25 parameters. k1 controls term-frequency saturation, b length normalisation.
_K1 = 1.5
_B = 0.75

#: Below this share of the top score a snippet is treated as noise and dropped,
#: which stops unrelated context from leaking into the prompt.
_RELATIVE_CUTOFF = 0.25

#: Applied to course-specific snippets when the user has not chosen a course.
#: Every course carries near-identical fees/batches/duration snippets, so an
#: unscoped "what are the fees" would otherwise surface whichever course happens
#: to score highest - an arbitrary answer to a question that was general. The
#: penalty is mild, so a query naming a course ("AI engineer fees") still wins
#: on its own lexical match.
_UNSCOPED_COURSE_PENALTY = 0.55


def _singular(word: str) -> str:
    """Fold a plural onto its singular so the two forms match.

    Without this, "what is the fee" retrieves nothing: the snippet is titled
    "fees", and BM25 sees two unrelated terms. It cost the most-asked question
    in the funnel a correct answer, and sent it to another course's snippet that
    happened to contain the phrase "no additional fee".

    Deliberately a handful of rules rather than a real stemmer. Index and query
    both pass through it, so consistency matters far more than linguistic
    correctness - "analytics" folding to "analytic" is harmless as long as it
    happens on both sides.
    """
    if len(word) <= 3 or not word.endswith("s") or word.endswith("ss"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"  # queries -> query
    if word.endswith(("sses", "ches", "shes", "xes", "zes")):
        return word[:-2]  # batches -> batch, classes -> class
    return word[:-1]  # fees -> fee, courses -> course


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens plus adjacent bigrams, singularised.

    Bigrams matter here: "power bi" and "data science" are single concepts, and
    without them a query for "power bi" scores every snippet containing "data".
    """
    words = [
        _singular(w)
        for w in _TOKEN_RE.findall(text.lower())
        if len(w) > 1 and w not in _STOPWORDS
    ]
    bigrams = [f"{a} {b}" for a, b in pairwise(words)]
    return words + bigrams


@runtime_checkable
class KnowledgeRetriever(Protocol):
    """Contract for any retrieval strategy."""

    def retrieve(
        self,
        query: str,
        *,
        course: str | None = None,
        audience: Audience = "all",
        limit: int | None = None,
        max_chars: int | None = None,
    ) -> list[KnowledgeSnippet]:
        """Return the snippets most relevant to `query`, best first."""
        ...


class KeywordRetriever:
    """BM25 retrieval with tag, course and audience boosting."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        *,
        default_limit: int = 6,
        default_max_chars: int = 6000,
    ) -> None:
        self._kb = knowledge_base
        self._default_limit = default_limit
        self._default_max_chars = default_max_chars
        self._index: dict[str, list[str]] = {}
        self._doc_freq: Counter[str] = Counter()
        self._avg_len: float = 1.0
        self._build_index()

    # ------------------------------------------------------------------ #
    def _build_index(self) -> None:
        """Tokenise every snippet once at startup."""
        total_len = 0
        for snippet in self._kb.snippets:
            # Title and tags carry more signal than body prose, so they are
            # repeated - a cheap, transparent field boost.
            tag_text = " ".join(sorted(snippet.tags))
            tokens = (
                tokenize(snippet.title) * 3
                + tokenize(tag_text) * 3
                + tokenize(snippet.content)
            )
            self._index[snippet.id] = tokens
            total_len += len(tokens)
            for term in set(tokens):
                self._doc_freq[term] += 1
        count = len(self._index) or 1
        self._avg_len = max(total_len / count, 1.0)
        logger.debug(
            "Retrieval index built",
            extra={"documents": count, "terms": len(self._doc_freq)},
        )

    def _idf(self, term: str) -> float:
        n = len(self._index) or 1
        df = self._doc_freq.get(term, 0)
        # Standard BM25 idf with the +1 smoothing that keeps it non-negative.
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def _score(self, tokens: list[str], query_terms: Counter[str]) -> float:
        if not tokens:
            return 0.0
        freqs = Counter(tokens)
        doc_len = len(tokens)
        score = 0.0
        for term, q_count in query_terms.items():
            tf = freqs.get(term, 0)
            if not tf:
                continue
            idf = self._idf(term)
            norm = tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * doc_len / self._avg_len))
            # A bigram hit is worth more than either of its words alone.
            term_weight = 1.6 if " " in term else 1.0
            score += idf * norm * q_count * term_weight
        return score

    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        query: str,
        *,
        course: str | None = None,
        audience: Audience = "all",
        limit: int | None = None,
        max_chars: int | None = None,
    ) -> list[KnowledgeSnippet]:
        limit = limit or self._default_limit
        max_chars = max_chars or self._default_max_chars

        query_terms = Counter(tokenize(query))
        scored: list[tuple[float, KnowledgeSnippet]] = []

        for snippet in self._kb.snippets:
            # Never surface post-sales content to a pre-sales user, or vice versa.
            if audience != "all" and snippet.audience not in ("all", audience):
                continue

            tokens = self._index.get(snippet.id, [])
            score = self._score(tokens, query_terms) if query_terms else 0.0

            if course:
                if snippet.course == course:
                    # Strongly prefer the course the user is actually asking about.
                    score = score * 1.8 + 0.5
                elif snippet.course is not None:
                    # Another course's details are usually a distraction.
                    score *= 0.35
            elif snippet.course is not None:
                # No course chosen yet - prefer answers that hold for all of them.
                score *= _UNSCOPED_COURSE_PENALTY
            score *= snippet.weight

            if score > 0:
                scored.append((score, snippet))

        if not scored:
            return self._fallback(course, audience, limit)

        scored.sort(key=lambda pair: pair[0], reverse=True)
        cutoff = scored[0][0] * _RELATIVE_CUTOFF

        selected: list[KnowledgeSnippet] = []
        used_chars = 0
        for score, snippet in scored:
            if score < cutoff or len(selected) >= limit:
                break
            size = len(snippet)
            if used_chars + size > max_chars and selected:
                break
            selected.append(snippet)
            used_chars += size

        logger.debug(
            "Knowledge retrieved",
            extra={
                "query": query[:120],
                "course": course,
                "audience": audience,
                "selected": [s.id for s in selected],
            },
        )
        return selected

    def _fallback(
        self, course: str | None, audience: Audience, limit: int
    ) -> list[KnowledgeSnippet]:
        """No lexical overlap - hand over a sensible default context.

        Better that the model sees the course overview and company basics than
        nothing at all, which is what would push it towards inventing an answer.
        """
        picks: list[KnowledgeSnippet] = []
        if course:
            overview = self._kb.get_snippet(f"course:{course}:overview")
            if overview is not None:
                picks.append(overview)
        for snippet_id in ("company:about", "company:contact"):
            snippet = self._kb.get_snippet(snippet_id)
            if snippet is not None and snippet not in picks:
                picks.append(snippet)
        return picks[:limit]


def build_retriever(
    knowledge_base: KnowledgeBase, *, limit: int, max_chars: int
) -> KnowledgeRetriever:
    """Factory - the single place to swap in a vector-backed retriever later."""
    return KeywordRetriever(
        knowledge_base, default_limit=limit, default_max_chars=max_chars
    )
