"""Generate console credentials.

    python -m scripts.console_password              # generate a strong password
    python -m scripts.console_password "my pass"    # hash one you chose

Prints the env vars to paste into Render. The plain password is shown once and
never stored anywhere - only the scrypt hash goes into the environment, so a
leaked env file does not hand over the console.
"""

from __future__ import annotations

import secrets
import string
import sys

from app.core.console_auth import hash_password

#: Ambiguous characters removed: this gets read off a screen and typed by hand.
_ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
    + "!@#$%^&*-_=+"
)


def generate(length: int = 20) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def main() -> None:
    supplied = sys.argv[1] if len(sys.argv) > 1 else None
    password = supplied or generate()

    print()
    print("=" * 68)
    print("  CONSOLE CREDENTIALS")
    print("=" * 68)
    print()
    print(f"  Username:  iScale-user")
    print(f"  Password:  {password}")
    print()
    if supplied is None:
        print("  Save the password now - it is not stored and cannot be recovered.")
        print("  Generate a new one and re-deploy if it is lost.")
        print()
    print("-" * 68)
    print("  Paste these into Render (Environment):")
    print("-" * 68)
    print()
    print("CONSOLE_ENABLED=true")
    print("CONSOLE_USERNAME=iScale-user")
    print(f"CONSOLE_PASSWORD_HASH={hash_password(password)}")
    print(f"CONSOLE_SESSION_SECRET={secrets.token_urlsafe(48)}")
    print()
    print("  The hash is safe to store; the password itself never is.")
    print("  Do NOT reuse the database password here - if this one leaks, the")
    print("  leads table should not leak with it.")
    print()


if __name__ == "__main__":
    main()
