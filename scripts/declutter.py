"""Strip injected non-JSON text from knowledge/*.json and report what was removed.

Something on this machine is intermittently writing prose into these files.
This recovers them: garbage prepended to or replacing a line is cut back to the
first structural character, and anything trailing the top-level object is
discarded.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

VALID_START = '"{}[],'

#: A complete `"key": value` pair at the start of a line, capturing whatever
#: follows it separately. Anything in group 2 on an otherwise-broken file is
#: injected prose, because valid JSON never puts loose text after a pair.
_PAIR_RE = re.compile(
    r'^(\s*"(?:[^"\\]|\\.)*"\s*:\s*'
    r'(?:"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)\s*,?)'
    r"(.*)$"
)


def clean(path: pathlib.Path) -> tuple[bool, list[str]]:
    raw = path.read_text(encoding="utf-8")
    notes: list[str] = []

    try:
        json.loads(raw)
        return False, notes
    except json.JSONDecodeError:
        pass

    # 1. Line-level repair: drop or trim lines that cannot be JSON.
    out: list[str] = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue

        if stripped[0] not in VALID_START:
            # Prose prepended to, or replacing, the real content.
            idx = min((line.find(c) for c in VALID_START if line.find(c) != -1), default=-1)
            if idx == -1:
                notes.append(f"line {lineno}: dropped {stripped[:60]!r}")
                continue
            notes.append(f"line {lineno}: trimmed leading junk from {stripped[:50]!r}")
            out.append("      " + line[idx:].strip())
            continue

        # Prose appended AFTER a valid "key": value pair - the line still starts
        # legally, so the check above waves it through. Keep the pair, drop the
        # tail.
        match = _PAIR_RE.match(line)
        if match and match.group(2).strip():
            notes.append(
                f"line {lineno}: dropped trailing junk {match.group(2).strip()[:50]!r}"
            )
            out.append(match.group(1).rstrip())
            continue

        out.append(line)
    text = "\n".join(out)

    # 2. Discard anything after the top-level object.
    try:
        obj, end = json.JSONDecoder().raw_decode(text.lstrip())
    except json.JSONDecodeError as exc:
        notes.append(f"UNRECOVERABLE: {exc}")
        return False, notes

    trailing = text.lstrip()[end:].strip()
    if trailing:
        notes.append(f"discarded {len(trailing)} trailing chars: {trailing[:60]!r}")

    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, notes


def clean_env(path: pathlib.Path) -> list[str]:
    """Strip injected prose from a .env file.

    Every line must be blank, a comment, or KEY=VALUE, and every value in this
    project is a single token. Prose lands either on its own line or appended
    after a value - the second is worse, because `WHATSAPP_APP_SECRET=abc123
    some words` parses as a secret that silently fails every signature check.
    """
    if not path.is_file():
        return []
    notes: list[str] = []
    out: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", stripped)
        if not match:
            notes.append(f"line {lineno}: dropped {stripped[:60]!r}")
            continue
        key, value = match.group(1), match.group(2)
        # Quoted values may legitimately contain spaces; bare ones may not.
        if not value.startswith(('"', "'")) and " " in value:
            head, _, tail = value.partition(" ")
            # `KEY=value  # explanation` is a normal inline comment, not damage.
            if not tail.lstrip().startswith("#"):
                notes.append(
                    f"line {lineno}: {key} - dropped trailing {tail.strip()[:50]!r}"
                )
                value = head
        out.append(f"{key}={value}")
    if notes:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return notes


def main() -> int:
    bad = 0
    for env_path in (pathlib.Path(".env"), pathlib.Path(".env.example")):
        notes = clean_env(env_path)
        if notes:
            print(f"=== {env_path.name}")
            for note in notes:
                print("   ", note)

    for path in sorted(pathlib.Path("knowledge").glob("*.json")):
        changed, notes = clean(path)
        if notes or changed:
            print(f"=== {path.name}")
            for note in notes:
                print("   ", note)
            if not changed:
                bad += 1
    print("\nfinal validation:")
    for path in sorted(pathlib.Path("knowledge").glob("*.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            print(f"   OK       {path.name}")
        except json.JSONDecodeError as exc:
            bad += 1
            print(f"   CORRUPT  {path.name}: {exc.msg} at line {exc.lineno}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
