"""Every `extra=` key must be one logging will actually accept.

Passing a reserved LogRecord field - `message`, `module`, `filename`, `args`
and friends - does not get ignored. `Logger.makeRecord` raises KeyError, so the
log line takes the whole request down with it.

It is a nasty failure mode because it is invisible in testing: the suite runs at
WARNING, where `logger.info(...)` returns before a record is ever built, so the
crash only appears in production. One of these 500'd the attachment endpoint,
which is what this file exists to stop happening again.
"""

from __future__ import annotations

import logging
import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Anything already on a LogRecord, plus the two the stdlib names explicitly.
_probe = logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None)
RESERVED = set(_probe.__dict__) | {"message", "asctime"}


def test_no_log_call_uses_a_reserved_extra_key() -> None:
    offenders: list[str] = []

    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"extra=\{([^}]*)\}", source, re.S):
            for key in re.findall(r"[\"'](\w+)[\"']\s*:", match.group(1)):
                if key in RESERVED:
                    line = source[: match.start()].count("\n") + 1
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{line} uses extra={key!r}"
                    )

    assert not offenders, (
        "reserved logging keys will raise KeyError at INFO level:\n  "
        + "\n  ".join(offenders)
    )
