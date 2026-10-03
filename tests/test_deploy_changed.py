"""Deployment decisions use the complete push and fail open without history."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_changed.py"
_spec = importlib.util.spec_from_file_location("deploy_changed", SCRIPT)
deploy_changed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_changed)


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo, changes):
    for name, content in changes.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "test revision")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Deployment Test")
    git(tmp_path, "config", "user.email", "deployment@example.invalid")
    # Tests must not inherit a developer's commit hooks or line-ending rules.
    hooks = tmp_path / ".git" / "empty-hooks"
    hooks.mkdir()
    git(tmp_path, "config", "core.hooksPath", str(hooks))
    git(tmp_path, "config", "core.autocrlf", "false")
    commit(tmp_path, {"README.md": "initial\n", "app/main.py": "initial = True\n"})
    return tmp_path


def cli(repo, *args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout in {"deploy=true\n", "deploy=false\n"}
    return result.stdout.strip()


@pytest.mark.parametrize("path", [
    "docs/DEPLOYING.md", "docs/nested/architecture.svg", "tests/test_feature.py",
    "tests/fixtures/example.json", "README.md", "CLAUDE.md", ".gitignore",
    ".gitattributes", "Makefile", "docker-compose.yml", ".github/workflows/test.yml",
    "package.json", "package-lock.json",
])
def test_known_non_runtime_paths_can_skip(path):
    assert not deploy_changed.runtime_path(path)


@pytest.mark.parametrize("path", [
    "app/main.py", "app/README.md", "alembic/versions/new.py", "alembic/README.md",
    "extension/overlay.js", "extension/README.md", "scripts/deploy-vps.sh",
    "scripts/README.md", "Dockerfile", ".dockerignore", "pyproject.toml",
    "alembic.ini", "docker-compose.prod.yml", ".env.example",
    ".github/workflows/deploy.yml", "caddy/Caddyfile", "new_directory/config.json",
    "new_directory/README.md", "new-runtime-file", "docs", "tests",
])
def test_runtime_and_unknown_paths_deploy(path):
    assert deploy_changed.runtime_path(path)


def test_documentation_and_test_only_push_skips_deployment(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"docs/deployment guide.md": "docs\n", "package.json": "{}\n"})
    head = commit(repo, {"tests/new_test.py": "pass\n", "README.md": "updated\n"})
    assert cli(repo, base, head) == "deploy=false"


def test_runtime_change_earlier_in_multi_commit_push_still_deploys(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"app/main.py": "initial = False\n"})
    head = commit(repo, {"docs/guide.md": "docs-only final commit\n"})
    assert cli(repo, base, head) == "deploy=true"


def test_newer_documentation_commit_does_not_supersede_waiting_runtime_release(repo):
    release = commit(repo, {"app/main.py": "release = True\n"})
    latest = commit(repo, {"docs/guide.md": "written after the runtime push\n"})
    git(repo, "update-ref", "refs/remotes/origin/main", latest)
    # The deployment job allows an already-tested image through when main has
    # advanced only in files that do not require a new image.
    assert cli(repo, release, "origin/main") == "deploy=false"


def test_newer_runtime_commit_supersedes_old_build_even_after_documentation_push(repo):
    release = git(repo, "rev-parse", "HEAD")
    commit(repo, {"app/main.py": "newer_release = True\n"})
    latest = commit(repo, {"docs/guide.md": "docs-only latest push\n"})
    git(repo, "update-ref", "refs/remotes/origin/main", latest)
    # A slow old build must not overwrite the newer runtime version. Compare
    # from that image's revision, rather than only the most recent commit.
    assert cli(repo, release, "origin/main") == "deploy=true"


def test_deleting_runtime_file_deploys(repo):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"app/main.py": None})
    assert cli(repo, base, head) == "deploy=true"


@pytest.mark.parametrize("source,destination", [
    ("app/main.py", "docs/retired.md"),
    ("docs/example.md", "app/example.md"),
])
def test_rename_considers_both_runtime_source_and_destination(repo, source, destination):
    base = commit(repo, {source: "identical content\n"})
    head = commit(repo, {source: None, destination: "identical content\n"})
    assert cli(repo, base, head) == "deploy=true"


def test_deleting_and_renaming_only_documentation_skips(repo):
    base = commit(repo, {"docs/old.md": "move me\n"})
    head = commit(repo, {"README.md": None, "docs/old.md": None, "docs/new.md": "move me\n"})
    assert cli(repo, base, head) == "deploy=false"


def test_git_filename_output_uses_nul_separators(repo):
    base = git(repo, "rev-parse", "HEAD")
    # Unix permits newlines; Windows still exercises Git's quoting of Unicode.
    name = "docs/a\nnew-file.md" if os.name == "posix" else "docs/café guide.md"
    head = commit(repo, {name: "documentation\n"})
    assert cli(repo, base, head) == "deploy=false"


def test_equal_commits_have_nothing_to_deploy(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert cli(repo, head, head) == "deploy=false"


@pytest.mark.parametrize("args", [
    (), ("", "HEAD"), ("0" * 40, "HEAD"), ("HEAD", "0" * 40), ("HEAD",),
    ("HEAD", ""), ("missing-base", "HEAD"), ("HEAD", "missing-head"),
    ("--force",), ("HEAD", "HEAD", "--force"),
])
def test_missing_history_and_forced_runs_deploy(repo, args):
    assert cli(repo, *args) == "deploy=true"


def test_outside_repository_deploys(tmp_path):
    assert cli(tmp_path, "HEAD~1", "HEAD") == "deploy=true"


@pytest.mark.parametrize("error", [OSError("git missing"), subprocess.TimeoutExpired("git", 30)])
def test_git_execution_failures_deploy(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(deploy_changed.subprocess, "run", fail)
    assert deploy_changed.should_deploy("base", "head")


def test_failed_diff_deploys_even_when_commits_resolve(monkeypatch):
    def fail_diff(*args, **kwargs):
        if args[0] == "diff":
            raise subprocess.CalledProcessError(128, ["git", *args])
        return b"a" * 40 + b"\n"

    monkeypatch.setattr(deploy_changed, "_git", fail_diff)
    assert deploy_changed.should_deploy("base", "head")
