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

    @property
    def group(self) -> str:
        """Which menu branch this course belongs to.

        Mirrors the site's own grouping: `cohort`, `advance`, `foundation`,
        `free`. Only `cohort` and `advance` are ever offered as menu rows -
        foundation and free courses are answered honestly when a user names one,
        but the funnel never volunteers them.
        """
        return str(self.raw.get("group", "")).strip().lower()

    @property
    def menu_label(self) -> str:
        """Short title for a menu button, falling back to the full name.

        WhatsApp caps a reply-button title at 20 characters and truncates
        anything longer with an ellipsis rather than raising, so every course
        that appears in a menu carries a hand-picked short label. The fallback
        keeps a newly added course visible instead of hiding it.
        """
        return str(self.raw.get("menu_label") or self.name).strip()

    @property
    def menu_order(self) -> int:
        """Position within the group's menu. Lower comes first.

        Explicit rather than alphabetical: the required order happens to match
        alphabetically today, so a rename would silently reshuffle a menu the
        business cares about.
        """
        try:
            return int(self.raw.get("menu_order", 99))
        except (TypeError, ValueError):
            return 99

    def summary_line(self) -> str:
        """One-line blurb for menus and confirmations."""
        bits = [self.short_description]
        if self.duration:
            bits.append(f"Duration: {self.duration}")
        return " ".join(b for b in bits if b).strip()

    def __repr__(self) -> str:
        # `raw` holds the whole course entry; printing it swamps any log line.
        return f"<Course slug={self.slug!r} name={self.name!r}>"
