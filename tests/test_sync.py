"""sync: this machine's journal files onto the repo's `journal` branch, checkout untouched."""
import json
import subprocess

import pytest

from opsjournal import sync as journal_sync


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def work(tmp_path, monkeypatch):
    """A work repo on a ticket branch with a bare origin, and a journal folder in it."""
    monkeypatch.setattr(journal_sync, "machine", lambda: "me@laptop")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))  # not the machine's hooks or settings
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        git(repo, "config", k, v)
    (repo / "README").write_text("x")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "init")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "checkout", "-qb", "CW-1")
    (repo / "journal").mkdir()
    (repo / "journal" / "actions.jsonl").write_text(json.dumps({"action": "note"}) + "\n")
    return repo, remote


def test_sync_pushes_a_journal_branch_without_touching_the_checkout(work):
    repo, remote = work
    result = journal_sync.sync(repo / "journal")
    assert result["status"] == "committed" and result["pushed"]
    assert git(remote, "show", "journal:machines/me@laptop/actions.jsonl") == '{"action": "note"}'
    assert git(repo, "branch", "--show-current") == "CW-1"
    assert git(repo, "status", "--porcelain") == "?? journal/"  # nothing staged, nothing committed on CW-1
    assert journal_sync.sync(repo / "journal")["status"] == "unchanged"


def test_sync_keeps_other_machines_folders(work):
    repo, remote = work
    journal_sync.sync(repo / "journal")
    other = repo.parent / "other"
    subprocess.run(["git", "clone", "-q", "-b", "journal", str(remote), str(other)], check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        git(other, "config", k, v)
    (other / "machines" / "runner@ci").mkdir(parents=True)
    (other / "machines" / "runner@ci" / "actions.jsonl").write_text("{}\n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "runner")
    git(other, "push", "-q", "origin", "journal")
    (repo / "journal" / "actions.jsonl").write_text('{"action": "note"}\n{"action": "k8s set"}\n')
    assert journal_sync.sync(repo / "journal")["pushed"]
    assert git(remote, "ls-tree", "-r", "--name-only", "journal").split() == [
        "machines/me@laptop/actions.jsonl", "machines/runner@ci/actions.jsonl"]
    assert "k8s set" in git(remote, "show", "journal:machines/me@laptop/actions.jsonl")


def test_no_push_and_not_a_repo(work, tmp_path):
    repo, remote = work
    assert journal_sync.sync(repo / "journal", push=False)["pushed"] is False
    assert git(remote, "branch", "--list") == ""
    (tmp_path / "loose").mkdir()
    with pytest.raises(journal_sync.SyncError):
        journal_sync.sync(tmp_path / "loose")


def test_background_sync_is_throttled_and_skipped_on_the_runner(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(journal_sync.subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.delenv("OPSJOURNAL_JOURNAL_SYNC", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    journal_sync.maybe_in_background(tmp_path, ["k8s", "set"])
    journal_sync.maybe_in_background(tmp_path, ["k8s", "set"])  # within SYNC_EVERY: not again
    assert len(started) == 1
    (tmp_path / "sync.json").unlink()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    journal_sync.maybe_in_background(tmp_path, ["k8s", "set"])
    monkeypatch.delenv("GITHUB_ACTIONS")
    journal_sync.maybe_in_background(None, ["k8s", "set"])
    journal_sync.maybe_in_background(tmp_path, ["sync"])
    assert len(started) == 1


def test_the_push_skips_pre_push_hooks(work):
    repo, remote = work
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_text(f"#!/bin/sh\ntouch {repo.parent / 'HOOK_RAN'}\nexit 1\n")
    (hooks / "pre-push").chmod(0o755)
    result = journal_sync.sync(repo / "journal")
    assert result["pushed"] and not (repo.parent / "HOOK_RAN").exists()
    assert git(remote, "show", "journal:machines/me@laptop/actions.jsonl") == '{"action": "note"}'
    assert git(repo, "branch", "--show-current") == "CW-1"


def test_a_shorter_local_journal_never_wipes_the_backup(work):
    repo, remote = work
    (repo / "journal" / "actions.jsonl").write_text('{"ts": "1"}\n{"ts": "2"}\n')
    journal_sync.sync(repo / "journal")
    (repo / "journal" / "actions.jsonl").write_text('{"ts": "3"}\n')  # a fresh clone: only what it wrote since
    result = journal_sync.sync(repo / "journal")
    assert result["merged"] == ["actions.jsonl"]
    assert git(remote, "show", "journal:machines/me@laptop/actions.jsonl").splitlines() == ['{"ts": "1"}', '{"ts": "2"}', '{"ts": "3"}']
    (repo / "journal" / "actions.jsonl").write_text('{"ts": "1"}\n{"ts": "2"}\n{"ts": "3"}\n{"ts": "4"}\n')
    assert "merged" not in journal_sync.sync(repo / "journal")  # the backup plus new lines: a plain update


def test_record_replaces_the_last_result():
    journal_sync.record({"ok": True, "pushed": True, "commit": "abc"})
    journal_sync.record({"ok": False, "error": "boom"})
    assert set(journal_sync.last()) == {"tried", "ok", "error", "at"}


def test_sync_backs_up_the_wrappers_files_and_never_drops_one(work, monkeypatch):
    from opsjournal import config

    repo, remote = work
    config.configure(extra_backup=lambda r: [(f"agents/{p.name}", p) for p in sorted((r / "agents").glob("*.md"))])
    (repo / "agents").mkdir()
    (repo / "agents" / "des.md").write_text("## How I want answers\n- short\n")
    assert journal_sync.sync(repo / "journal")["pushed"]
    assert "- short" in git(remote, "show", "journal:machines/me@laptop/agents/des.md")
    (repo / "agents" / "des.md").unlink()  # a fresh clone without it
    journal_sync.sync(repo / "journal")
    assert "- short" in git(remote, "show", "journal:machines/me@laptop/agents/des.md")  # still in the backup
