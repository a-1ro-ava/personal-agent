#!/usr/bin/env python3
# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Copy the shared package (src/shared/pa_shared) into the agent and collector apps.

Each app is deployed from its own folder, so the shared code is vendored into both.

Usage:
    uv run scripts/sync_shared.py          # copy
    uv run scripts/sync_shared.py --check  # exit 1 when a copy is out of date
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

EXIT_SUCCESS = 0
EXIT_FAILURE = 1

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "src" / "shared" / "pa_shared"
TARGETS = [REPO_ROOT / "src" / "agent" / "pa_shared", REPO_ROOT / "src" / "functions" / "pa_shared"]


def out_of_date(target: Path) -> list[str]:
    """Return the shared files that differ from or are missing in target."""
    stale = []
    for src in SOURCE.glob("*.py"):
        dst = target / src.name
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            stale.append(str(dst.relative_to(REPO_ROOT)))
    return stale


def create_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(description="Vendor src/shared/pa_shared into each app.")
    parser.add_argument("--check", action="store_true", help="Only report whether copies are up to date.")
    return parser


def main() -> int:
    """Main entry point."""
    args = create_parser().parse_args()
    if args.check:
        stale = [f for t in TARGETS for f in out_of_date(t)]
        for f in stale:
            print(f"out of date: {f}", file=sys.stderr)
        return EXIT_FAILURE if stale else EXIT_SUCCESS
    for target in TARGETS:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns("__pycache__"))
        print(f"synced {target.relative_to(REPO_ROOT)}")
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
