"""The CLI's commands running now on this machine, so another shell or agent can see and follow them.

A run writes <journal>/running/<run id>.json when it starts (journal.begin): its id, argv (secrets as ***), who,
session, pid, cwd, branch and ticket, and its target once it resolves one. The file is removed when the run ends
(journal.finish), after its end line is in commands.jsonl. A run killed with SIGKILL never removes it, so a file
whose pid is no longer a process of the CLI is stale: `listing()` drops and deletes it.

Logs: begin sets $<PREFIX>_RUN_LOGS to <journal>/running/<run id>.logs; anything the run starts (a script on a
remote host, say) appends the path of each log it writes to that file, before it writes to the log.
`opsjournal follow` tails those; the end line in commands.jsonl keeps them as `logs`.

Every write here is best effort: nothing in this module may fail or noticeably slow down the command.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from opsjournal import config

FOLDER = "running"
TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]+-\d+)(?![0-9])")


def logs_env() -> str:
    return config.settings.env("RUN_LOGS")


def _not_listed(argv: list[str]) -> bool:
    """Watching runs isn't a run worth watching."""
    prefix = list(config.settings.journal_argv)
    return any(argv[:len(prefix) + 1] == [*prefix, word] for word in ("running", "follow"))


def folder(journal_dir: Optional[Path]) -> Optional[Path]:
    return journal_dir / FOLDER if journal_dir else None


def branch_of(cwd: str) -> Optional[str]:
    """The git branch checked out in `cwd`, read from .git/HEAD (no git process: this runs on every command)."""
    try:
        here = Path(cwd).resolve()
        for d in (here, *here.parents):
            dot = d / ".git"
            if dot.is_dir():
                head = dot / "HEAD"
            elif dot.is_file():
                text = dot.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir:"):
                    return None
                gitdir = Path(text.split(":", 1)[1].strip())
                head = (gitdir if gitdir.is_absolute() else d / gitdir) / "HEAD"
            else:
                continue
            ref = head.read_text(encoding="utf-8").strip()
            return ref.removeprefix("ref: refs/heads/") if ref.startswith("ref: refs/heads/") else None
    except (OSError, ValueError):
        return None
    return None


def ticket_of(branch: Optional[str]) -> Optional[str]:
    match = TICKET.search(branch or "")
    return match.group(1) if match else None


def _write(path: Path, entry: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)  # a reader never sees half a file


def start(journal_dir: Optional[Path], entry: dict, argv: list[str]) -> None:
    """Write running/<id>.json for this run and point $<PREFIX>_RUN_LOGS at its log list. Never raises."""
    where = folder(journal_dir)
    if where is None or _not_listed(argv):
        return
    try:
        cwd = os.getcwd()
        branch = branch_of(cwd)
        entry = {**entry, "pid": os.getpid(), "cwd": cwd, "branch": branch, "ticket": ticket_of(branch)}
        where.mkdir(parents=True, exist_ok=True)
        _write(where / f"{entry['invocation']}.json", {k: v for k, v in entry.items() if v not in (None, "", [], {})})
        os.environ[logs_env()] = str(where / f"{entry['invocation']}.logs")
    except Exception:  # noqa: BLE001 - never fail the command over its journal
        return


def update(journal_dir: Optional[Path], run_id: str, **fields) -> None:
    """Add fields (the target it resolved) to a running run's file, if it has one. Never raises."""
    where = folder(journal_dir)
    if where is None:
        return
    path = where / f"{run_id}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if all(entry.get(k) == v for k, v in fields.items()):
            return
        _write(path, {**entry, **fields})
    except Exception:  # noqa: BLE001 - not listed (journal running/follow), unreadable: nothing to update
        return


def logs_of(journal_dir: Optional[Path], run_id: str) -> list[str]:
    """The log files a run has written so far (from running/<id>.logs), oldest first."""
    where = folder(journal_dir)
    try:
        text = (where / f"{run_id}.logs").read_text(encoding="utf-8") if where else ""
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        if line.strip() and line.strip() not in out:
            out.append(line.strip())
    return out


def end(journal_dir: Optional[Path], run_id: str, restore_env: Optional[str]) -> list[str]:
    """Remove a run's running files; return its logs (for the end line). Never raises."""
    logs = logs_of(journal_dir, run_id)
    _remove(journal_dir, run_id)
    if restore_env is None:
        os.environ.pop(logs_env(), None)
    else:
        os.environ[logs_env()] = restore_env
    return logs


def _remove(journal_dir: Optional[Path], run_id: str) -> None:
    where = folder(journal_dir)
    if where is not None:
        for suffix in (".json", ".logs"):
            try:
                (where / f"{run_id}{suffix}").unlink()
            except OSError:
                pass


def alive(pid: int) -> bool:
    """Whether `pid` is still a process of the CLI (a python one): a pid reused by something else counts as gone."""
    if pid == os.getpid():  # this very process (its name can be `pytest` or a wrapper on Linux)
        return True
    if not _pid_alive(pid):
        return False
    try:
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    name = comm.strip().lower()
    return not name or "python" in name or config.settings.cli in name


def listing(journal_dir: Optional[Path], skip: Optional[str] = None) -> list[dict]:
    """The runs going on now, oldest first, each with its `logs`. Files of runs that died without ending (SIGKILL,
    a crash of the machine) are deleted on the way. `skip`: a run id to leave out (the caller's own)."""
    where = folder(journal_dir)
    if where is None or not where.is_dir():
        return []
    out = []
    for path in where.glob("*.json"):
        run_id = path.stem
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or run_id == skip:
            continue
        if not alive(int(entry.get("pid") or 0)):
            _remove(journal_dir, run_id)
            continue
        out.append({**entry, "logs": logs_of(journal_dir, run_id)})
    return sorted(out, key=lambda r: r.get("ts", ""))


def is_running(journal_dir: Optional[Path], run_id: str, pid: int) -> bool:
    where = folder(journal_dir)
    return bool(where) and (where / f"{run_id}.json").is_file() and alive(pid)


# ---- following one run ------------------------------------------------------------------------------------


def end_line(journal_dir: Optional[Path], run_id: str) -> Optional[dict]:
    """A run's line in commands.jsonl (written when it ended), searched from the end of the file."""
    path = journal_dir / "commands.jsonl" if journal_dir else None
    if not path or not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            for chunk in (1 << 18, size):  # the last 256 KB has it unless the run ended long ago
                fh.seek(max(0, size - chunk))
                for raw in reversed(fh.read().splitlines()):
                    if run_id.encode() not in raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("invocation") == run_id:
                        return row
                if chunk >= size:
                    break
    except OSError:
        return None
    return None


def follow(journal_dir: Optional[Path], run: dict, emit, timeout: Optional[float] = None, poll: float = 0.5) -> Optional[int]:
    """Pass the run's logs to `emit` (text, as it is written) until the run ends; return its exit code: the code on
    its end line, 124 when `timeout` seconds pass first, None when it died without an end line (SIGKILL)."""
    import time

    run_id, pid = run["invocation"], int(run.get("pid") or 0)
    offsets: dict[str, int] = {}

    def drain(paths: list[str]) -> None:
        for name in paths:
            try:
                with open(name, "rb") as fh:
                    fh.seek(offsets.get(name, 0))
                    data = fh.read()
                    offsets[name] = fh.tell()
            except OSError:
                continue
            if data:
                emit(data.decode("utf-8", errors="replace"))

    deadline = time.monotonic() + timeout if timeout else None
    where = folder(journal_dir)
    while True:
        going = bool(where) and (where / f"{run_id}.json").is_file() and _pid_alive(pid)
        drain(logs_of(journal_dir, run_id))
        if not going:
            line = end_line(journal_dir, run_id)
            if line is None:
                _remove(journal_dir, run_id)
                return None
            drain(line.get("logs") or [])
            code = line.get("exit")
            return code if isinstance(code, int) else 1
        if deadline and time.monotonic() >= deadline:
            return 124
        time.sleep(poll)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False
    return True
