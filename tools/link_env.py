#!/usr/bin/env python3
"""Create this project's `.env` from the sibling OAI provider's.

Why a script rather than a shell one-liner: the value is a secret, and this way
it goes from one file to another without ever being printed, echoed into a
transcript, or held in an agent's context. Only a masked confirmation is shown.

    python3 tools/link_env.py --source ~/.config/myfind/secrets.env

Idempotent: refuses to overwrite an existing `.env` unless --force is given.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

# An existing `.env` to copy the credential FROM, so a value already on this
# machine is never retyped (or pasted into a shell history). Point this at
# whatever file already holds it.
SOURCE_DEFAULT = Path.home() / ".config" / "myfind" / "secrets.env"
TARGET_DEFAULT = Path(__file__).resolve().parent.parent / ".env"

# Which key in the source maps to which key here.
MAPPING = {"OAI_BASEX_PASSWORD": "MYFIND_BASEX_PASSWORD"}


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]} ({len(value)} chars)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=SOURCE_DEFAULT)
    ap.add_argument("--target", type=Path, default=TARGET_DEFAULT)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not args.source.exists():
        print(f"no source .env at {args.source}")
        return 1
    if args.target.exists() and not args.force:
        print(f"{args.target} already exists; leaving it alone (use --force to rewrite)")
        return 0

    source = read_env(args.source)
    lines = ["# myfind local secrets. Untracked, mode 600.", ""]
    found = []
    for src_key, dst_key in MAPPING.items():
        value = source.get(src_key, "")
        if not value:
            print(f"WARNING: {src_key} not found in {args.source}")
            lines.append(f"# {dst_key}=   <- NOT FOUND in {args.source}")
            continue
        lines.append(f"{dst_key}={value}")
        found.append(dst_key)

    if not found:
        print("nothing to copy; refusing to write an empty .env")
        return 1

    args.target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Owner-only: this file is the one thing in the repo that must never be read
    # by another account.
    args.target.chmod(stat.S_IRUSR | stat.S_IWUSR)

    print(f"wrote {args.target} (mode {oct(args.target.stat().st_mode)[-3:]})")
    for dst_key in found:
        print(f"  {dst_key} = {mask(read_env(args.target)[dst_key])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
