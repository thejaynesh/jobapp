#!/usr/bin/env python3
"""Print a GitHub Actions deployment decision for the whole pushed commit range.

Usage: python scripts/deploy_changed.py [BASE] [HEAD] [--force]

Only known documentation and development paths can skip deployment. Missing
history, a first push, manual dispatch, or a Git error always deploys instead.
"""

import argparse
import os
from pathlib import Path
import subprocess


NON_RUNTIME_FILES = frozenset({
    ".gitignore", ".gitattributes", "Makefile", "docker-compose.yml",
    ".github/workflows/test.yml", "package.json", "package-lock.json",
})


def runtime_path(path: str) -> bool:
    """Unknown paths count as runtime; Markdown under app/ still deploys."""
    if path.startswith(("docs/", "tests/")) or path in NON_RUNTIME_FILES:
        return False
    return not ("/" not in path and path.endswith(".md"))


def _git(*args: str, cwd: str | Path | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=30,
    ).stdout


def should_deploy(
    base: str | None, head: str | None, *, force: bool = False,
    cwd: str | Path | None = None,
) -> bool:
    if force or any(not ref or not ref.strip("0") for ref in (base, head)):
        return True
    try:
        # Resolve to commits first so malformed refs cannot become diff flags
        # or accidentally compare the working tree instead of the pushed range.
        commits = [
            _git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", cwd=cwd)
            .decode("ascii").strip()
            for ref in (base, head)
        ]
        changed = _git(
            "diff", "--name-only", "--no-renames", "-z", *commits, "--", cwd=cwd,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return True
    # NUL separation also handles spaces and newlines in tracked filenames.
    # Disabling rename detection checks both the removed and added path.
    return any(runtime_path(os.fsdecode(path)) for path in changed.split(b"\0") if path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?")
    parser.add_argument("head", nargs="?")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    decision = should_deploy(args.base, args.head, force=args.force)
    print(f"deploy={str(decision).lower()}")


if __name__ == "__main__":
    main()
