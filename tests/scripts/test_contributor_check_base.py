"""Regression coverage for contributor-attribution commit boundaries."""

import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci.contributor_check_base import commit_range_base


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str, email: str) -> str:
    marker = repo / "history.txt"
    marker.write_text(f"{message}\n", encoding="utf-8")
    _git(repo, "add", "history.txt")
    subprocess.run(
        ["git", "-c", "core.hooksPath=", "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": email,
        },
    )
    return _git(repo, "rev-parse", "HEAD")


def test_fixture_ignores_host_global_hook_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = tmp_path / "host-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(f"[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "synthetic commit", "test@example.com")


def test_stacked_pr_base_excludes_commits_already_on_non_main_base(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "main", "main@example.com")
    stacked_base = _commit(tmp_path, "parent PR", "historical-unmapped@example.com")
    _commit(tmp_path, "child PR", "310557894+peltmonger-leo@users.noreply.github.com")

    lower_bound = commit_range_base(stacked_base)
    emails = _git(tmp_path, "log", f"{lower_bound}..HEAD", "--format=%ae", "--no-merges").splitlines()

    assert emails == ["310557894+peltmonger-leo@users.noreply.github.com"]


def test_non_pr_run_uses_main_merge_base_when_no_pr_base_is_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.ci.contributor_check_base.run_git",
        lambda *args: "main-merge-base",
    )

    assert commit_range_base("") == "main-merge-base"
