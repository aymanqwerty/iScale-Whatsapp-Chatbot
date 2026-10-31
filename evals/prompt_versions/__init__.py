"""Selectable prompt versions for the eval.

Each module here exposes the same two callables the application uses -
``build_system_prompt`` and ``build_user_prompt`` - so the runner can point the
pipeline at any of them without changing anything else. The runner inspects each
``build_system_prompt`` signature and passes only the keyword arguments that
version actually accepts, which is what lets an old prompt with a smaller
signature run beside the current one.

- ``current``    -> re-exports the live, shipping prompt (app.services.llm.prompts)
- ``v1_initial`` -> the first prompt, vendored verbatim from git commit 79ff074
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: Human-ordered, oldest first. Used by ``--compare`` when no pair is given.
KNOWN_VERSIONS: tuple[str, ...] = ("v1_initial", "current")


def load_version(name: str) -> ModuleType:
    """Import a prompt version module by short name.

    Raises a clear error rather than a bare ImportError so a typo in ``--version``
    names the versions that do exist.
    """
    try:
        return importlib.import_module(f"evals.prompt_versions.{name}")
    except ModuleNotFoundError as exc:  # pragma: no cover - CLI guard
        known = ", ".join(KNOWN_VERSIONS)
        raise SystemExit(
            f"Unknown prompt version {name!r}. Available versions: {known}."
        ) from exc
