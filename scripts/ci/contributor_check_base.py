#!/usr/bin/env python3
"""Select the lower commit bound for contributor-attribution checks."""

from __future__ import annotations

import subprocess
import sys


def run_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_range_base(pr_base_sha: str) -> str:
    """Use a PR's configured base SHA, falling back to main for non-PR runs."""
    if pr_base_sha:
        return pr_base_sha
    return run_git("merge-base", "origin/main", "HEAD")


def main() -> int:
    print(commit_range_base(sys.argv[1] if len(sys.argv) > 1 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
