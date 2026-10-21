"""Back the journal up to a `journal` branch of the git repo it sits in, so it survives losing this computer.

Each machine owns one folder on that branch, `machines/<user>@<host>/`, holding copies of its journal files
(every *.jsonl, and the wrapper's Settings.extra_backup files). A sync builds the commit with git plumbing (its own index file, never
the checkout), on top of the remote branch, replaces this machine's folder, and pushes. Other machines and the
runner own other folders, so a sync never conflicts; a push that loses a race is retried once. The push skips
pre-push hooks (--no-verify): the branch is never checked out, and the global hook would rebase the checked-out one.

The CLI syncs by itself in the background after a run, at most every SYNC_EVERY seconds (<PREFIX>_JOURNAL_SYNC=off
turns that off); `opsjournal sync` does it now. A lost machine's journal comes back with
`git -C REPO show origin/journal:machines/<user>@<host>/actions.jsonl`.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from opsjournal import config

SYNC_EVERY = 15 * 60


class SyncError(RuntimeError):
    pass


def machine() -> str:
    raw = f"{os.environ.get('GITHUB_ACTOR') and 'runner' or getpass.getuser()}@{socket.gethostname().split('.')[0]}"
    return re.sub(r"[^A-Za-z0-9@._-]", "-", raw)


def _git(repo: Path, *args: str, env: Optional[dict] = None, check: bool = True, input_bytes: Optional[bytes] = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, input=input_bytes,
                          env={**os.environ, **(env or {})}, timeout=120)
    out, err = proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")
    if check and proc.returncode:
        raise SyncError(f"git {' '.join(args[:2])}: {(err or out).strip()[-300:]}")
    return out.strip()


def _git_bytes(repo: Path, *args: str) -> Optional[bytes]:
    """A blob's exact bytes, None when it isn't there."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120)
    return proc.stdout if proc.returncode == 0 else None


def union_lines(first: bytes, second: bytes) -> bytes:
    """Every distinct line of both, ordered by each line's `ts` (stable, so lines without one keep their place)."""
    seen, lines = set(), []
    for line in first.splitlines() + second.splitlines():
        if line.strip() and line not in seen:
            seen.add(line)
            lines.append(line)

    def ts(line: bytes) -> str:
        try:
            return str(json.loads(line).get("ts", ""))
        except (ValueError, AttributeError):
            return ""

    return b"".join(line + b"\n" for line in sorted(lines, key=ts))


def repo_of(folder: Path) -> Optional[Path]:
    """The git repo the journal folder sits in (the work repo), None when it isn't in one."""
    try:
        top = _git(folder, "rev-parse", "--show-toplevel")
    except (SyncError, OSError):
        return None
    return Path(top) if top else None


def sync(folder: Path, push: bool = True, remote: str = "origin") -> dict:
    """Commit this machine's journal files to the `journal` branch (on top of the remote one) and push it."""
    BRANCH = config.settings.branch
    repo = repo_of(folder)
    if repo is None:
        raise SyncError(f"{folder} is not inside a git repo: the journal can only be backed up from one")
    files = sorted(p for p in folder.glob("*.jsonl") if p.is_file())
    notes = config.settings.extra_backup(repo)  # (path in the backup, file): kept as they are
    if not files and not notes:
        return {"status": "nothing", "machine": machine()}
    remote_ref = f"refs/remotes/{remote}/{BRANCH}"
    has_remote = remote in _git(repo, "remote", check=False).split()
    for attempt in (1, 2):
        if push and has_remote:
            _git(repo, "fetch", "--quiet", remote, f"+refs/heads/{BRANCH}:{remote_ref}", check=False)
        base = None
        for ref in (remote_ref, f"refs/heads/{BRANCH}"):
            if _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False):
                base = _git(repo, "rev-parse", ref)
                break
        prefix = f"machines/{machine()}/"
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if base:
                _git(repo, "read-tree", base, env=env)
            merged = []  # nothing is removed from the backup: a file missing here (a fresh clone) stays there
            for path in files:
                data = path.read_bytes()
                backed_up = _git_bytes(repo, "cat-file", "blob", f"{base}:{prefix}{path.name}") if base else None
                if backed_up and not data.startswith(backed_up):
                    # this machine's file is not the backup plus new lines (a re-cloned ~/work, a new Mac with the
                    # same name): keep every line of both instead of replacing the backup
                    data = union_lines(backed_up, data)
                    merged.append(path.name)
                blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=data)
                _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{prefix}{path.name}", env=env)
            for name, path in notes:
                blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=path.read_bytes())
                _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{prefix}{name}", env=env)
            tree = _git(repo, "write-tree", env=env)
        extra = {"merged": merged} if merged else {}
        if base and _git(repo, "rev-parse", f"{base}^{{tree}}") == tree:
            commit, status = base, "unchanged"
        else:
            lines = sum(len(p.read_bytes().splitlines()) for p in files)
            message = f"journal: {machine()} ({lines} lines)"
            name, email = config.settings.sync_author
            commit = _git(repo, "commit-tree", tree, *(["-p", base] if base else []), "-m", message,
                          env={"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
            _git(repo, "update-ref", f"refs/heads/{BRANCH}", commit)
            status = "committed"
        on_remote = commit == _git(repo, "rev-parse", "--verify", "--quiet", remote_ref, check=False)
        if not (push and has_remote) or on_remote:
            return {"status": status, "pushed": False, "commit": commit, "machine": machine(), "repo": str(repo), **extra}
        # --no-verify: pre-push hooks guard the branch you're on (the global one rebases the checked-out branch onto
        # main); this pushes a ref that is never checked out, so they have nothing to check and must not run
        proc = subprocess.run(["git", "-C", str(repo), "push", "--quiet", "--no-verify", remote, f"{commit}:refs/heads/{BRANCH}"],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            _git(repo, "update-ref", remote_ref, commit, check=False)
            return {"status": status, "pushed": True, "commit": commit, "machine": machine(), "repo": str(repo), **extra}
        if attempt == 2:
            raise SyncError(f"git push {remote} {BRANCH}: {(proc.stderr or proc.stdout).strip()[-300:]}")
    raise SyncError("unreachable")


# ---- background sync after a run ----------------------------------------------------------------------------


def state_file() -> Path:
    return config.settings.state_file()


def _state() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def record(result: dict) -> None:
    """This attempt's result replaces the last one's (only the throttle's `tried` carries over)."""
    try:
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text(json.dumps({"tried": _state().get("tried", 0), **result, "at": time.time()}))
    except OSError:
        pass


def last() -> dict:
    return _state()


def maybe_in_background(folder: Optional[Path], argv: list[str]) -> None:
    """After a run: start `journal sync` detached when the last attempt is older than SYNC_EVERY."""
    settings = config.settings
    off = settings.env("JOURNAL_SYNC")
    if folder is None or os.environ.get(off, "").lower() in ("0", "off", "false", "no"):
        return
    prefix = list(settings.journal_argv)
    if os.environ.get("GITHUB_ACTIONS") == "true" or argv[:len(prefix) + 1] == [*prefix, "sync"]:
        return
    if time.time() - float(_state().get("tried", 0)) < SYNC_EVERY:
        return
    state = state_file()
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({**_state(), "tried": time.time()}))
        log = (state.parent / "journal-sync.log").open("a")
        command = settings.sync_argv or [sys.executable, "-m", "opsjournal", "sync", "--quiet"]
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         env={**os.environ, off: "off"})
    except OSError:
        pass
