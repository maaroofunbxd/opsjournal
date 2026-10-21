"""The journal: what a CLI ran and what it changed on live systems, and by whom (you, an AI agent, or a runner).

Files, in the journal folder (config.Settings.folder):

  commands.jsonl     every run of the CLI: the command, who, the target it resolved, exit code, seconds
  actions.jsonl      every write (a command wrapped in @journaled), its outcome (ok / failed / declined /
                     error), and notes (`opsjournal note`) for what was done outside the CLI
  <yours>.jsonl      a wrapper's own writers (append(stream, entry)), named in Settings.streams
  running/           one file per run going on now (running.py: `opsjournal running` / `follow`)
  prompts.jsonl      the messages sent to agents, from a prompt hook (prompts.py)

Every line says `by` (the OS / GitHub user) and `actor`: `user`, `runner`, or `agent:<tool>` (agentboard's
identity: <its prefix>_ACTOR=agent:<name>, or the variables an agent tool sets). Writing never fails the command.
The files are backed up to a git branch (sync.py): in the background after a run, or `opsjournal sync`.
"""
from __future__ import annotations

import datetime
import functools
import getpass
import json
import os
import re
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from agentboard.board import nudge
from agentboard.identity import actor, session_id

from opsjournal import config
from opsjournal import running as journal_running

COMMANDS, ACTIONS = "commands", "actions"

SECRET = re.compile(r"token|passw|secret|api[-_]?key", re.I)

_run: dict[str, Any] = {}  # this process's run: id, start, argv, target, writes
_extra: list[dict] = []  # fields the running action adds (journal.add), innermost last


def journal_dir() -> Optional[Path]:
    return config.settings.folder()


def streams() -> tuple:
    """The files with changes in them (commands.jsonl is every run, so it isn't one)."""
    return config.settings.streams


def who() -> dict:
    """The fields every journal line carries: when, who, which run (parent = the run that started this one)."""
    out: dict[str, Any] = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by": os.environ.get("GITHUB_ACTOR") or getpass.getuser(),
        "actor": actor(),
    }
    sid = session_id()
    if sid:
        out["session"] = sid
    if _run.get("id"):
        out["invocation"] = _run["id"]
    parent = _run.get("parent") if _run.get("id") else os.environ.get(config.settings.env("PARENT"))
    if parent:
        out["parent"] = parent
    if os.environ.get("GITHUB_RUN_ID"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        out["run"] = f"{server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return out


ENV_PAIR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def mask_pair(arg: str) -> str:
    """KEY=VALUE -> KEY=*** (anything else as it is)."""
    match = ENV_PAIR.match(arg)
    return f"{match.group(1)}=***" if match else arg


def redact_argv(argv: list[str]) -> list[str]:
    """Hide secrets: the value after a flag named like one (--token X, --api-key=X), a KEY=VALUE whose KEY is
    named like one (DB_PASSWORD=x), and every KEY=VALUE of the argv shapes Settings.env_value_argv names (env values
    are often secrets whatever their name)."""
    env_values = config.settings.env_value_argv(argv)
    out, hide = [], False
    for arg in argv:
        match = ENV_PAIR.match(arg)
        if hide:
            out.append("***")
            hide = False
        elif arg.startswith("-") and SECRET.search(arg.split("=", 1)[0]):
            out.append(arg.split("=", 1)[0] + "=***" if "=" in arg else arg)
            hide = "=" not in arg
        elif match and (env_values or SECRET.search(match.group(1))):
            out.append(mask_pair(arg))
        else:
            out.append(arg)
    return out


def source() -> str:
    return shlex.join([Path(sys.argv[0]).name, *redact_argv(sys.argv[1:])])


def append(stream: str, entry: dict) -> None:
    """One line in <journal>/<stream>.jsonl, with who() first. Never raises."""
    folder = journal_dir()
    if folder is None:
        return
    line = {**who(), "source": source(), **entry}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{stream}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, separators=(",", ":"), sort_keys=True, default=str) + "\n")
    except OSError as exc:
        print(f"WARNING: could not write journal {folder / (stream + '.jsonl')}: {exc}", file=sys.stderr)
    if stream in streams():
        count_write()


def count_write() -> None:
    """This run changed something (commands.jsonl's `writes`); a wrapper writing its own lines calls it."""
    if _run.get("id"):
        _run["writes"] = _run.get("writes", 0) + 1


# ---- the run: one line in commands.jsonl per invocation of the CLI ------------------------------------


def _skip(argv: list[str]) -> bool:
    completing = any(k.startswith((f"_{config.settings.env_prefix}_COMPLETE", "_TYPER_COMPLETE")) for k in os.environ)
    return completing or not argv or any(a in ("--help", "-h", "--install-completion", "--show-completion") for a in argv)


def begin(argv: list[str]) -> None:
    """Start recording this run; child processes of the same CLI (task steps) get it as their parent."""
    _run.clear()
    if _skip(argv):
        return
    outer = os.environ.get(config.settings.env("PARENT"))
    _run.update(id=uuid.uuid4().hex[:12], start=time.monotonic(), argv=redact_argv(argv), target={}, parent=outer or None,
                outer_env=outer)
    os.environ[config.settings.env("PARENT")] = _run["id"]  # what child runs (task steps) record as their parent
    _run["outer_logs"] = os.environ.get(journal_running.logs_env())
    try:  # running/<id>.json until finish: `journal running` / `follow` read it
        journal_running.start(journal_dir(), {**who(), "argv": _run["argv"]}, argv)
    except Exception:  # noqa: BLE001 - never fail the command over its journal
        pass


def current_run() -> Optional[str]:
    """This process's run id (None when it isn't recorded: --help, completion)."""
    return _run.get("id")


def target(**fields: Optional[str]) -> None:
    """Where a command works (region, site, cluster, host: any names), for the run and its action."""
    known = {k: v for k, v in fields.items() if v}
    if _run.get("id"):
        _run["target"].update(known)
        if known:
            try:
                journal_running.update(journal_dir(), _run["id"], **_run["target"])
            except Exception:  # noqa: BLE001 - never fail the command over its journal
                pass
    if _extra:
        _extra[-1].update({k: v for k, v in known.items() if k not in _extra[-1]})


def finish(code: int) -> None:
    if not _run.get("id"):
        return
    entry = {
        "argv": _run["argv"],
        "cwd": os.getcwd(),
        "tty": sys.stdin.isatty(),
        "exit": code,
        "seconds": round(time.monotonic() - _run["start"], 1),
        **_run["target"],
    }
    if _run.get("writes"):
        entry["writes"] = _run["writes"]
    if _run.get("error"):
        entry["error"] = _run["error"]
    folder = journal_dir()
    logs = journal_running.logs_of(folder, _run["id"])
    if logs:
        entry["logs"] = logs
    append(COMMANDS, entry)
    journal_running.end(folder, _run["id"], _run.get("outer_logs"))  # after the end line: `follow` reads it next
    argv = _run["argv"]
    nudge(argv)
    if _run.get("outer_env") is None:
        os.environ.pop(config.settings.env("PARENT"), None)
    else:
        os.environ[config.settings.env("PARENT")] = _run["outer_env"]
    _run.clear()
    from opsjournal import sync as journal_sync

    journal_sync.maybe_in_background(journal_dir(), argv)  # back it up to the journal branch


def run_main(app: Callable[[], Any]) -> None:
    """Run the typer app, recording the run (exit code included) in commands.jsonl."""
    begin(sys.argv[1:])
    code = 0
    try:
        app()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code and isinstance(exc.__context__, Exception) and not _is_cli_exit(exc.__context__):
            _run["error"] = _error_text(exc.__context__)  # a crash the CLI turned into exit 1
        raise
    except KeyboardInterrupt:
        code = 130
        raise
    except BaseException as exc:
        code = 1
        _run["error"] = _error_text(exc)
        raise
    finally:
        finish(code)


def _is_cli_exit(exc: BaseException) -> bool:
    """typer.Exit / Abort / a usage error: the command chose its exit code, nothing crashed."""
    names = {c.__name__ for c in type(exc).__mro__}
    return bool(names & {"Exit", "Abort", "ClickException", "UsageError", "BadParameter"})


def _error_text(exc: BaseException) -> str:
    """What crashed, and where: the exception and the innermost frame of Settings.package, short enough for one line."""
    import traceback

    frames = [f for f in traceback.extract_tb(exc.__traceback__) if config.settings.package in f.filename]
    where = f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    return f"{type(exc).__name__}: {exc}"[:300] + where


# ---- actions: one line in actions.jsonl per write ---------------------------------------------------


def add(**fields: Any) -> None:
    """Extra fields for the action being recorded (before/after, image, run id, ...)."""
    if _extra:
        _extra[-1].update(fields)


def _params(kwargs: dict, mask: tuple[str, ...] = ()) -> dict:
    out = {}
    for name, value in kwargs.items():
        if value is None or value is False or value == [] or value == () or name in ("dry_run",):
            continue
        if SECRET.search(name):
            value = "***"
        elif name in mask and isinstance(value, (list, tuple)):
            value = [mask_pair(str(v)) for v in value]
        elif isinstance(value, (list, tuple)):
            value = [str(v)[:300] for v in value]
        elif not isinstance(value, (bool, int, float)):
            value = str(value)[:500]
        out[name] = value
    return out


def _current_context():
    """The running command's context (its command_path names the action): click's, or the copy newer typer ships."""
    try:
        from click import get_current_context
    except ImportError:
        from typer._click.globals import get_current_context
    return get_current_context(silent=True)


def journaled(func: Optional[Callable] = None, *, when: Optional[Callable[[dict], bool]] = None, mask: tuple[str, ...] = ()):
    """Record a write command in actions.jsonl: its params, target and outcome, also when it fails or the
    question is declined. Skipped for --dry-run, and when `when(kwargs)` says this call writes nothing.
    `mask`: list params whose KEY=VALUE items are recorded as KEY=*** (env values)."""

    def wrap(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if kwargs.get("dry_run") or (when and not when(kwargs)):
                return fn(*args, **kwargs)
            import typer

            ctx = _current_context()
            action = ctx.command_path.split(" ", 1)[-1] if ctx else fn.__name__
            _extra.append({})
            start, status, code, error = time.monotonic(), "ok", 0, None
            try:
                return fn(*args, **kwargs)
            except typer.Abort:
                status = "declined"
                raise
            except typer.Exit as exc:
                code = exc.exit_code
                status = "ok" if code == 0 else "failed"
                raise
            except KeyboardInterrupt:
                status = "interrupted"
                raise
            except Exception as exc:  # noqa: BLE001  (recorded, then re-raised)
                status, error = "error", f"{type(exc).__name__}: {exc}"[:300]
                raise
            finally:
                extra = _extra.pop()
                entry = {"action": action, "status": status, "params": _params(kwargs, mask), **extra}
                if code:
                    entry["exit"] = code
                if error:
                    entry["error"] = error
                entry["seconds"] = round(time.monotonic() - start, 1)
                append(ACTIONS, entry)

        return wrapper

    return wrap(func) if func else wrap


def note(text: str, commands: list[str] | None = None, **fields: Any) -> dict:
    """Something done outside the CLI (a raw command, a UI, a teammate): `opsjournal note`."""
    entry = {"action": "note", "status": "ok", "note": text, **{k: v for k, v in fields.items() if v}}
    if commands:
        entry["commands"] = commands
    append(ACTIONS, entry)
    return entry


# ---- reading ----------------------------------------------------------------------------------------


def read(stream: str) -> list[dict]:
    folder = journal_dir()
    path = folder / f"{stream}.jsonl" if folder else None
    if not path or not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            row["_stream"] = stream
            rows.append(row)
    return rows


def since(value: str) -> str:
    """'2h' / '3d' / '30m' / '2026-10-10' -> an ISO timestamp to compare `ts` against."""
    match = re.fullmatch(r"(\d+)\s*([mhd])", value.strip())
    if match:
        n, unit = int(match.group(1)), match.group(2)
        delta = datetime.timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})
        return (datetime.datetime.now(datetime.timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return datetime.date.fromisoformat(value.strip()).strftime("%Y-%m-%dT00:00:00Z")
    except ValueError as exc:
        raise ValueError(f"--since takes 30m, 2h, 3d or a date (2026-10-10), not {value!r}") from exc


def summary(row: dict) -> str:
    """One line for a change, whichever file it came from (a wrapper's own files: Settings.summarize)."""
    own = config.settings.summarize(row)
    if own is not None:
        return own
    if row.get("action") == "note":
        cmds = f" ({len(row['commands'])} command(s))" if row.get("commands") else ""
        return f"note: {row.get('note', '')}{cmds}"
    params = row.get("params") or {}
    shown = " ".join(
        str(v) if k in config.settings.bare_params else f"{k}={v}"
        for k, v in params.items()
        if k not in config.settings.hidden_params
    )
    return f"{row.get('action', '?')} {shown}"[:160]


def target_of(row: dict) -> str:
    """Where a row's command worked (Settings.where)."""
    return config.settings.where(row)


def searchable(row: dict) -> str:
    """Everything `--grep` looks at in a line, lowercased: the command, what changed, where, notes, who."""
    params = row.get("params") or {}
    parts = [row.get("source", ""), " ".join(row.get("argv") or []), target_of(row), str(row.get("actor", "")),
             str(row.get("note", "")), " ".join(row.get("commands") or []), str(row.get("ticket", "")),
             " ".join(f"{k}={v}" for k, v in params.items())]
    if row.get("_stream") != COMMANDS:
        parts.append(summary(row))
    return " ".join(str(p) for p in parts).lower()


def grep_match(row: dict, patterns: list[str]) -> bool:
    """True when any pattern matches; a pattern matches when all its words are in the line (any order, any case),
    and none of its `-word`s are: `-g "pr-test gcpapac"` both words, `-g a -g b` either, `-g "config -revert"`."""
    if not patterns:
        return True
    text = searchable(row)
    for pattern in patterns:
        words = pattern.lower().split()
        want = [w for w in words if not (w.startswith("-") and len(w) > 1 and not w.startswith("--"))]
        avoid = [w[1:] for w in words if w not in want]
        if all(w in text for w in want) and not any(w in text for w in avoid):
            return True
    return False


class AmbiguousId(ValueError):
    pass


def invocation(run_id: str) -> Optional[dict]:
    """One run by its id (or a unique prefix): the run, the writes it made, and the runs it started (task steps).
    None when no line has it; AmbiguousId when the prefix matches several runs."""
    runs = read(COMMANDS)
    ids = sorted({r["invocation"] for r in runs if str(r.get("invocation", "")).startswith(run_id)})
    writes_all = [r for stream in streams() for r in read(stream)]
    if not ids:  # a write whose run line is missing (the run crashed before finishing) is still found
        ids = sorted({r["invocation"] for r in writes_all if str(r.get("invocation", "")).startswith(run_id)})
    if not ids:
        return None
    if len(ids) > 1:
        raise AmbiguousId(f"{run_id!r} matches {len(ids)} runs ({', '.join(ids[:5])}{', ...' if len(ids) > 5 else ''}): give more of the id")
    rid = ids[0]
    run = next((r for r in runs if r.get("invocation") == rid), None)
    writes = sorted((r for r in writes_all if r.get("invocation") == rid), key=lambda r: r.get("ts", ""))
    children = sorted((r for r in runs if r.get("parent") == rid), key=lambda r: r.get("ts", ""))
    return {"id": rid, "run": run, "writes": writes, "children": children}
