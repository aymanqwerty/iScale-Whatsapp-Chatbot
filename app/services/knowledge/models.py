"""Value objects for the knowledge base."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Audience = Literal["all", "pre_sales", "post_sales"]

#: Where a snippet came from. Used for provenance in the prompt and for
#: light source-based weighting during retrieval.
SourceKind = Literal["course", "faq", "policy", "company", "placements"]


@dataclass(frozen=True, slots=True)
class KnowledgeSnippet:
    """One retrievable unit of knowledge.

    Courses are split into several snippets (fees, duration, curriculum, …)
    rather than kept whole, so a question about duration does not drag the entire
    syllabus into the prompt.
    """

    id: str
    title: str
    content: str
    source: SourceKind
    tags: frozenset[str] = frozenset()
    course: str | None = None
    audience: Audience = "all"
    #: Static importance multiplier applied on top of the relevance score.
    weight: float = 1.0

    def render(self) -> str:
        prefix = f"[{self.source}]"
        return f"{prefix} {self.title}\n{self.content}"

    def __len__(self) -> int:
        return len(self.title) + len(self.content)


@dataclass(frozen=True, slots=True)
class Course:
    """Structured access to one course entry."""

    slug: str
    name: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def short_description(self) -> str:
        return str(self.raw.get("short_description", ""))

    @property
    def duration(self) -> str:
        return str(self.raw.get("duration", ""))

    @property
    def keywords(self) -> tuple[str, ...]:
        return tuple(str(k).lower() for k in self.raw.get("keywords", []))

    @property
    def featured(self) -> bool:
        """Whether the course gets its own row in the WhatsApp course menu.

        WhatsApp caps a list at ten rows, and a wall of choices converts worse
        than a short one. Non-featured courses stay fully answerable - they are
        just reached through the "Other courses" row instead.
        """
        return bool(self.raw.get("featured", False))

    def summary_line(self) -> str:
        """One-line blurb for menus and confirmations."""
        bits = [self.short_description]
        if self.duration:
            bits.append(f"Duration: {self.duration}")
        return " ".join(b for b in bits if b).strip()

    def __repr__(self) -> str:
        # `raw` holds the whole course entry; printing it swamps any log line.
        return f"<Course slug={self.slug!r} name={self.name!r}>"
