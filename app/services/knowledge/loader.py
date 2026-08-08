"""Load the JSON knowledge files and flatten them into retrievable snippets.

The files are read once at startup and held in memory - they are small, and a
restart is an acceptable way to pick up an edit for the MVP. `reload()` exists
so an admin endpoint or a file watcher can refresh without a restart later.

Adding a new JSON file means adding one `_load_*` method; nothing else changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from app.core.exceptions import KnowledgeBaseError
from app.core.logging import get_logger
from app.services.knowledge.models import Audience, Course, KnowledgeSnippet

logger = get_logger(__name__)


def _as_text(value: Any) -> str:
    """Flatten an arbitrary JSON value into readable prose for the prompt."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        parts = [_as_text(item) for item in value]
        return "; ".join(p for p in parts if p)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            rendered = _as_text(item)
            if rendered:
                parts.append(f"{str(key).replace('_', ' ')}: {rendered}")
        return "; ".join(parts)
    return str(value)


def _format_hours(working_hours: dict[str, Any]) -> str:
    """Render the working-hours block as a plain sentence.

    `_as_text` would flatten the nested day objects into
    "monday: open: 11:00; close: 19:00", which reads badly in a prompt and
    invites the model to garble a closing time. Days are emitted in week order,
    not JSON order, so "which days are you open" gets a sensible answer.
    """
    days = working_hours.get("business_hours")
    if not isinstance(days, dict):
        return _as_text(working_hours)

    order = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    known = [d for d in order if d in days] + [d for d in days if d not in order]

    parts: list[str] = []
    for day in known:
        spec = days.get(day)
        label = str(day).capitalize()
        if not isinstance(spec, dict) or spec.get("closed"):
            parts.append(f"{label} closed")
        elif spec.get("open") and spec.get("close"):
            parts.append(f"{label} {spec['open']}-{spec['close']}")
    rendered = "; ".join(parts)

    timezone = working_hours.get("timezone")
    return f"{rendered} ({timezone})" if timezone and rendered else rendered


def _grouped(
    document: dict[str, Any], *, list_key: str
) -> list[tuple[str, Any]]:
    """Yield `(category, entries)` pairs from a grouped or flat document.

    A flat `{"faqs": [...]}` yields one pair with an empty category; a grouped
    `{"admission": [...], "classes": [...]}` yields one pair per key. Supporting
    both means a restructured file degrades loudly (wrong tags) rather than
    silently (nothing loaded).
    """
    flat = document.get(list_key)
    if isinstance(flat, list):
        return [("", flat)]
    return [(key, value) for key, value in document.items() if isinstance(value, list)]


_AUDIENCES: frozenset[str] = frozenset({"all", "pre_sales", "post_sales"})


def _audience(value: Any) -> Audience:
    """Coerce an author-supplied audience to a valid one, defaulting to `all`.

    A typo in the JSON should widen the audience, never hide the answer entirely.
    """
    text = str(value or "all").strip().lower()
    return cast(Audience, text) if text in _AUDIENCES else "all"


def _slugify(entry: dict[str, Any]) -> str:
    """Course id: the explicit `slug`, else the name kebab-cased."""
    raw = str(entry.get("slug") or entry.get("name", ""))
    return raw.strip().lower().replace(" ", "-")


class KnowledgeBase:
    """In-memory knowledge base: structured entities plus flat snippets."""

    def __init__(
        self,
        *,
        courses: list[Course],
        snippets: list[KnowledgeSnippet],
        company: dict[str, Any],
        placements: dict[str, Any],
        rules: dict[str, Any] | None = None,
        prompt_overrides: dict[str, Any] | None = None,
        documents: dict[str, Any] | None = None,
    ) -> None:
        self._courses = courses
        self._courses_by_slug = {c.slug: c for c in courses}
        self._snippets = snippets
        self._snippets_by_id = {s.id: s for s in snippets}
        self._company = company
        self._placements = placements
        self._rules = rules or {}
        self._prompt_overrides = prompt_overrides or {}
        self._documents = documents or {}

    # --- structured access -------------------------------------------------
    @property
    def courses(self) -> list[Course]:
        return list(self._courses)

    @property
    def featured_courses(self) -> list[Course]:
        """Courses shown directly in the menu.

        Falls back to every course when nothing is flagged, so an unflagged
        `courses.json` still produces a working menu rather than an empty one.
        """
        featured = [c for c in self._courses if c.featured]
        return featured or list(self._courses)

    @property
    def other_courses(self) -> list[Course]:
        """Courses reached through the "Other courses" row.

        Empty when nothing is flagged featured - in that case `featured_courses`
        already lists everything, and offering "Other courses" would lead to an
        empty menu.
        """
        if not any(course.featured for course in self._courses):
            return []
        return [course for course in self._courses if not course.featured]

    @property
    def snippets(self) -> list[KnowledgeSnippet]:
        return list(self._snippets)

    @property
    def company(self) -> dict[str, Any]:
        return dict(self._company)

    @property
    def placements(self) -> dict[str, Any]:
        return dict(self._placements)

    @property
    def rules(self) -> dict[str, Any]:
        """`chatbot_rules.json` - persona and guardrails, owned by the business.

        Not retrievable knowledge: these shape *how* the bot answers, so they go
        into the system prompt on every turn rather than into the index.
        """
        return dict(self._rules)

    @property
    def prompt_overrides(self) -> dict[str, Any]:
        """`prompts.json` - objectives and standing reminders for the prompt."""
        return dict(self._prompt_overrides)

    @property
    def documents(self) -> dict[str, Any]:
        """Raw parsed JSON per filename, for cross-file consistency checks."""
        return dict(self._documents)

    @property
    def company_name(self) -> str:
        # `company.json` nests the profile under a "company" key, but tolerate a
        # flat document too so a hand-edited file cannot blank the bot's name.
        profile = self._company.get("company")
        if isinstance(profile, dict) and profile.get("name"):
            return str(profile["name"])
        return str(self._company.get("name") or "iScale")

    def get_course(self, slug: str | None) -> Course | None:
        if not slug:
            return None
        return self._courses_by_slug.get(slug)

    def get_snippet(self, snippet_id: str) -> KnowledgeSnippet | None:
        return self._snippets_by_id.get(snippet_id)

    def snippets_for_course(self, slug: str) -> list[KnowledgeSnippet]:
        return [s for s in self._snippets if s.course == slug]

    def match_course(self, text: str) -> Course | None:
        """Best-effort course lookup from free text ("i want power bi")."""
        needle = text.strip().lower()
        if not needle:
            return None
        if needle in self._courses_by_slug:
            return self._courses_by_slug[needle]
        for course in self._courses:
            if course.name.lower() == needle or course.slug.replace("-", " ") == needle:
                return course
        # A course's own name always beats another course's keyword list -
        # "power bi" appears in the Data Analytics keywords, but the user asking
        # for it means the Power BI course.
        for terms_of in (self._identity_terms, self._keyword_terms):
            candidates: list[tuple[int, Course]] = []
            for course in self._courses:
                for term in terms_of(course):
                    if term and term in needle:
                        candidates.append((len(term), course))
            if candidates:
                # Longest match first, so "data science" wins over "data".
                candidates.sort(key=lambda pair: pair[0], reverse=True)
                return candidates[0][1]
        return None

    @staticmethod
    def _identity_terms(course: Course) -> tuple[str, ...]:
        return (course.name.lower(), course.slug.replace("-", " "))

    @staticmethod
    def _keyword_terms(course: Course) -> tuple[str, ...]:
        return course.keywords

    def __len__(self) -> int:
        return len(self._snippets)


class KnowledgeLoader:
    """Reads `knowledge/*.json` and builds a `KnowledgeBase`."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    # ------------------------------------------------------------------ #
    def load(self) -> KnowledgeBase:
        if not self._dir.is_dir():
            raise KnowledgeBaseError(
                f"Knowledge directory not found: {self._dir}", path=str(self._dir)
            )

        # The whole document, not just its "company" block - contact details,
        # working hours and the learning-format section are siblings of it.
        company = self._read("company.json")
        placements = self._read("placements.json").get("placements", {})
        raw_courses = self._read("courses.json").get("courses", [])
        # Both files are read whole: they are grouped by category/section now,
        # and were flat lists under a "faqs"/"policies" key before. The snippet
        # builders accept either, so an edit to the shape cannot silently drop
        # every entry the way it did before.
        raw_faqs = self._read("faqs.json")
        raw_policies = self._read("policies.json")
        # Persona and prompt guidance. These steer every answer rather than
        # being retrieved for particular questions, so they are kept aside from
        # the snippet index and injected into the system prompt instead.
        rules = self._read("chatbot_rules.json")
        prompt_overrides = self._read("prompts.json")
        callback_rules = self._read("callback_rules.json")

        courses = [
            Course(slug=_slugify(entry), name=str(entry.get("name", "")).strip(), raw=entry)
            for entry in raw_courses
            if entry.get("name")
        ]

        snippets: list[KnowledgeSnippet] = []
        for course in courses:
            snippets.extend(self._course_snippets(course))
        snippets.extend(self._faq_snippets(raw_faqs))
        snippets.extend(self._policy_snippets(raw_policies))
        snippets.extend(self._company_snippets(company))
        snippets.extend(self._placement_snippets(placements))

        logger.info(
            "Knowledge base loaded",
            extra={
                "courses": len(courses),
                "snippets": len(snippets),
                "directory": str(self._dir),
            },
        )
        return KnowledgeBase(
            courses=courses,
            snippets=snippets,
            company=company,
            placements=placements,
            rules=rules,
            prompt_overrides=prompt_overrides,
            documents={
                "callback_rules.json": callback_rules,
                "policies.json": raw_policies,
                "company.json": company,
            },
        )

    # ------------------------------------------------------------------ #
    def _read(self, filename: str) -> dict[str, Any]:
        path = self._dir / filename
        if not path.is_file():
            logger.warning("Knowledge file missing, skipping", extra={"file": filename})
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KnowledgeBaseError(
                f"{filename} is not valid JSON: {exc}", file=filename
            ) from exc
        if not isinstance(data, dict):
            raise KnowledgeBaseError(f"{filename} must contain a JSON object", file=filename)
        return data

    # --- snippet builders --------------------------------------------- #
    def _course_snippets(self, course: Course) -> list[KnowledgeSnippet]:
        """Split one course into field-level snippets.

        Each `(key, label, tags)` triple becomes its own snippet when present,
        which is what lets a "how long is it?" question retrieve the duration
        without the full curriculum.
        """
        raw = course.raw
        base_tags = {course.slug, *course.name.lower().split(), *course.keywords}
        out: list[KnowledgeSnippet] = []

        overview_bits = [
            raw.get("short_description", ""),
            f"Duration: {raw['duration']}" if raw.get("duration") else "",
            f"Mode: {raw['mode']}" if raw.get("mode") else "",
            f"Level: {raw['level']}" if raw.get("level") else "",
        ]
        out.append(
            KnowledgeSnippet(
                id=f"course:{course.slug}:overview",
                title=f"{course.name} - overview",
                content="\n".join(b for b in overview_bits if b),
                source="course",
                tags=frozenset(base_tags | {"overview", "about", "course", "detail"}),
                course=course.slug,
                weight=1.2,
            )
        )

        fields: tuple[tuple[str, str, set[str]], ...] = (
            ("duration", "duration", {"duration", "long", "months", "weeks", "time"}),
            ("effort", "weekly effort", {"effort", "hours", "week", "commitment"}),
            ("mode", "delivery mode", {"mode", "online", "offline", "live", "class"}),
            ("eligibility", "eligibility", {"eligibility", "prerequisite", "who", "background"}),
            ("fees", "fees", {"fees", "price", "cost", "payment", "emi", "instalment"}),
            ("tools", "tools covered", {"tools", "technology", "software", "stack"}),
            ("curriculum", "curriculum", {"curriculum", "syllabus", "topics", "modules"}),
            ("projects", "projects", {"projects", "portfolio", "capstone", "hands on"}),
            ("batches", "batches", {"batch", "weekend", "weekday", "timing", "schedule"}),
            ("certification", "certification", {"certificate", "certification"}),
            ("career_outcomes", "career outcomes", {"career", "job", "role", "outcome", "salary"}),
            (
                "career_support",
                "career support",
                {"placement", "resume", "interview", "portfolio", "mentorship", "support"},
            ),
            ("mentors", "mentors", {"mentor", "trainer", "faculty", "teacher", "instructor"}),
            ("url", "course page", {"link", "url", "website", "page", "details"}),
        )
        for key, label, extra_tags in fields:
            content = _as_text(raw.get(key))
            if not content:
                continue
            out.append(
                KnowledgeSnippet(
                    id=f"course:{course.slug}:{key}",
                    title=f"{course.name} - {label}",
                    content=content,
                    source="course",
                    tags=frozenset(base_tags | extra_tags),
                    course=course.slug,
                )
            )
        return out

    def _faq_snippets(self, document: dict[str, Any]) -> list[KnowledgeSnippet]:
        """Build FAQ snippets from either supported layout.

        New: `{"admission": [{question, keywords, answer}, ...], ...}`
        Old: `{"faqs": [{id, question, answer, tags}, ...]}`
        """
        out: list[KnowledgeSnippet] = []
        for category, entries in _grouped(document, list_key="faqs"):
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                question = str(entry.get("question", "")).strip()
                answer = str(entry.get("answer", "")).strip()
                if not question or not answer:
                    continue
                # `keywords` is the new spelling of `tags`; accept both.
                terms = entry.get("keywords") or entry.get("tags") or []
                tags = {str(t).lower() for t in terms}
                if category:
                    tags.add(category.lower())
                out.append(
                    KnowledgeSnippet(
                        id=str(entry.get("id") or f"faq:{category or 'general'}:{index}"),
                        title=question,
                        content=answer,
                        source="faq",
                        tags=frozenset(tags),
                        course=entry.get("course"),
                        audience=_audience(entry.get("audience")),
                        weight=1.1,
                    )
                )
        return out

    def _policy_snippets(self, document: dict[str, Any]) -> list[KnowledgeSnippet]:
        """Build policy snippets from either supported layout.

        New: `{"refund_policy": {available, message}, "working_hours": {...}}`
        Old: `{"policies": [{id, title, content, tags}, ...]}`
        """
        entries = document.get("policies")
        if isinstance(entries, list):
            return [
                KnowledgeSnippet(
                    id=str(entry.get("id") or f"policy:{index}"),
                    title=str(entry.get("title", "Policy")),
                    content=content,
                    source="policy",
                    tags=frozenset(str(t).lower() for t in entry.get("tags", [])),
                    weight=1.15,
                )
                for index, entry in enumerate(entries)
                if isinstance(entry, dict)
                and (content := str(entry.get("content", "")).strip())
            ]

        out: list[KnowledgeSnippet] = []
        for section, body in document.items():
            content = _as_text(body)
            if not content:
                continue
            words = section.replace("_", " ").split()
            out.append(
                KnowledgeSnippet(
                    id=f"policy:{section}",
                    title=section.replace("_", " ").capitalize(),
                    content=content,
                    source="policy",
                    # The section name is the strongest signal a user's wording
                    # will match ("refund" -> refund_policy), so it is a tag too.
                    tags=frozenset({*(w.lower() for w in words), "policy", "rule"}),
                    weight=1.15,
                )
            )
        return out

    def _company_snippets(self, document: dict[str, Any]) -> list[KnowledgeSnippet]:
        """Build company snippets from the whole `company.json` document.

        Sections are pulled by name where they exist and merged with any
        matching top-level keys, so both the nested layout and an older flat
        one produce the same snippets. Anything absent is skipped rather than
        emitted empty.
        """
        if not document:
            return []

        profile = document.get("company")
        profile = profile if isinstance(profile, dict) else document

        def section(name: str) -> dict[str, Any]:
            value = document.get(name)
            return value if isinstance(value, dict) else {}

        def pick(source: dict[str, Any], *keys: str) -> dict[str, Any]:
            return {k: source[k] for k in keys if source.get(k) not in (None, "", [], {})}

        out: list[KnowledgeSnippet] = []

        about = pick(
            profile,
            "description",
            "about",
            "tagline",
            "mission",
            "vision",
            "founded_year",
            "founded",
            "previous_name",
            "industry",
            "company_type",
            "service_location",
            "office_locations",
            "locations",
            "website",
        )
        if about:
            out.append(
                KnowledgeSnippet(
                    id="company:about",
                    title=f"About {self._name_of(document)}",
                    content=_as_text(about),
                    source="company",
                    tags=frozenset(
                        {
                            "about", "company", "iscale", "who", "institute",
                            "mission", "vision", "founded", "history", "location",
                            "office", "city", "website",
                        }
                    ),
                    weight=1.1,
                )
            )

        contact = {**section("contact"), **pick(profile, "phone", "email", "website")}
        hours = section("working_hours")
        if hours:
            contact["working hours"] = _format_hours(hours)
        if contact:
            out.append(
                KnowledgeSnippet(
                    id="company:contact",
                    title="Contact details and working hours",
                    content=_as_text(contact),
                    source="company",
                    tags=frozenset(
                        {
                            "contact", "hours", "timing", "time", "open", "closed",
                            "holiday", "address", "location", "email", "phone",
                            "whatsapp", "reach", "call",
                        }
                    ),
                    weight=1.15,
                )
            )

        learning = {
            **section("learning"),
            **pick(document, "target_audience", "supported_course_categories"),
        }
        if learning:
            out.append(
                KnowledgeSnippet(
                    id="company:learning",
                    title="How the learning works",
                    content=_as_text(learning),
                    source="company",
                    tags=frozenset(
                        {
                            "mode", "online", "offline", "live", "recorded", "class",
                            "language", "hindi", "english", "certificate", "mentor",
                            "internship", "audience", "who", "format", "lms",
                        }
                    ),
                )
            )

        social = section("social")
        if social:
            out.append(
                KnowledgeSnippet(
                    id="company:social",
                    title="Website and social links",
                    content=_as_text(social),
                    source="company",
                    tags=frozenset(
                        {"link", "website", "linkedin", "youtube", "social", "url", "channel"}
                    ),
                )
            )
        return out

    @staticmethod
    def _name_of(document: dict[str, Any]) -> str:
        profile = document.get("company")
        if isinstance(profile, dict) and profile.get("name"):
            return str(profile["name"])
        return str(document.get("name") or "the company")

    def _placement_snippets(self, placements: dict[str, Any]) -> list[KnowledgeSnippet]:
        if not placements:
            return []
        tags = frozenset(
            {
                "placement",
                "job",
                "career",
                "hiring",
                "salary",
                "package",
                "interview",
                "resume",
                "referral",
            }
        )
        return [
            KnowledgeSnippet(
                id="placements:support",
                title="Placement support",
                content=_as_text(
                    {
                        k: placements.get(k)
                        for k in ("overview", "services", "eligibility", "roles", "disclaimer")
                        if placements.get(k)
                    }
                ),
                source="placements",
                tags=tags,
                weight=1.1,
            ),
            KnowledgeSnippet(
                id="placements:salary",
                title="Salary expectations",
                content=_as_text(
                    {
                        k: placements.get(k)
                        for k in ("salary_note", "hiring_partner_note", "disclaimer")
                        if placements.get(k)
                    }
                ),
                source="placements",
                tags=frozenset({"salary", "package", "ctc", "pay", "lpa", "earning", "income"}),
            ),
        ]


def load_knowledge_base(directory: Path) -> KnowledgeBase:
    """Convenience wrapper used at application startup."""
    return KnowledgeLoader(directory).load()
