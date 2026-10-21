"""`opsjournal`: the record of what was run and changed, and by whom (you, an AI agent, a runner).
See journal.py. A wrapper (a CLI that records its runs here) usually has its own `journal` command group with its
own filters; this one works on any journal folder.

  opsjournal changes                       # what changed on live systems (newest last), before -> after, notes
  opsjournal changes --actor agent --since 1d -w prod
  opsjournal executions --since 2h         # every run, reads too, with exit code and run id
  opsjournal executions -g "deploy api" --actor agent   # search: all words, any order (-g a -g b: either)
  opsjournal show 3f9c2a                   # one run: its command, exit, the writes it made, the runs it started
  opsjournal running                       # the commands running now, in any shell or session
  opsjournal follow --last                 # print its log as it grows; exit with its exit code
  opsjournal note "scaled api to 3 by hand" -w prod --cmd "kubectl scale deploy/api --replicas 3"
  opsjournal sync                          # back it up to the journal branch now
  opsjournal prompts --repo api --since 1d # the messages you sent to agents (a prompt hook runs `opsjournal prompt`)
"""
from __future__ import annotations

import subprocess
import sys
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from opsjournal import config, journal

console = Console()

journal_app = typer.Typer(
    help="What was run and changed, and by whom (you, an AI agent, a runner): changes, executions, show, running, follow, note, sync (backup), prompts (what you asked agents).",
    no_args_is_help=True,
)


def print_json(value) -> None:
    import json

    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _cmd(*words: str) -> str:
    return config.settings.command(*words)


def _ran(argv: list) -> str:
    return " ".join([config.settings.cli, *argv])

LimitOpt = typer.Option(30, "--limit", "-n", min=1, help="Newest N lines.")
SinceOpt = typer.Option(None, "--since", help="30m, 2h, 3d or a date (2026-10-10).")
ActorOpt = typer.Option(None, "--actor", "-a", help="user | runner | agent (any agent) | agent:<tool> ...")
WhereFilter = typer.Option(None, "--where", "-w", help="Only rows whose target (the `where` column) contains this.")
GrepOpt = typer.Option([], "--grep", "-g", help="Search the command, what changed, where, notes: all words of one -g, any order, "
                       "any case; -word excludes; several -g match any. E.g. -g 'pr-test gcpapac' -g 'config -revert'.")


def _keep(row: dict, since: Optional[str], actor: Optional[str], where: Optional[str]) -> bool:
    if since and row.get("ts", "") < since:
        return False
    if actor and not str(row.get("actor", "user")).startswith(actor):
        return False
    return not where or where in journal.target_of(row)


def _filter(rows: list[dict], limit: int, since: Optional[str], actor: Optional[str], where: Optional[str],
            grep: Optional[List[str]] = None) -> list[dict]:
    try:
        cutoff = journal.since(since) if since else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since") from exc
    rows = [r for r in rows if _keep(r, cutoff, actor, where) and journal.grep_match(r, grep or [])]
    return sorted(rows, key=lambda r: r.get("ts", ""))[-limit:]


def _where_written() -> str:
    folder = journal.journal_dir()
    return str(folder) if folder else config.settings.off_reason


@journal_app.command("changes")
def cmd_changes(
    limit: int = LimitOpt,
    since: Optional[str] = SinceOpt,
    actor: Optional[str] = ActorOpt,
    where: Optional[str] = WhereFilter,
    action: Optional[str] = typer.Option(None, "--action", help="Only actions containing this (note, ...)."),
    grep: List[str] = GrepOpt,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Every recorded change, oldest to newest: the writes of every journaled command, and notes.

    Writes that failed or were declined at the question are listed too (status column)."""
    rows = [r for stream in journal.streams() for r in journal.read(stream)]
    if action:
        rows = [r for r in rows if action in journal.summary(r)]
    rows = _filter(rows, limit, since, actor, where, grep)
    if json_output:
        print_json(rows)
        return
    if not rows:
        console.print(f"[yellow]Nothing recorded{' matching' if any((since, actor, where, action, grep)) else ''}. Journal: {_where_written()}[/yellow]")
        return
    table = Table(show_lines=False)
    for col in ("when (UTC)", "run", "actor", "where", "what", "status"):
        table.add_column(col, overflow="fold")
    for r in rows:
        status = r.get("status", "")
        status = "ok" if isinstance(status, int) and 200 <= status < 300 else str(status)  # an HTTP status
        style = {"ok": "green", "declined": "yellow"}.get(status, "red")
        table.add_row(r.get("ts", ""), str(r.get("invocation", ""))[:6], str(r.get("actor", r.get("by", ""))), journal.target_of(r),
                      journal.summary(r), f"[{style}]{status}[/{style}]")
    console.print(table)
    console.print(f"[dim]{_where_written()}  ({_cmd('show')} RUN for one run; --json for every field)[/dim]")
    console.print(f"[dim]{_backup_line()}[/dim]")


def _backup_line() -> str:
    import datetime

    from opsjournal import sync as journal_sync

    last = journal_sync.last()
    if "ok" not in last:
        return f"backup: never synced ({_cmd('sync')})"
    when = datetime.datetime.fromtimestamp(last.get("at", 0)).strftime("%Y-%m-%d %H:%M")
    if last["ok"]:
        return f"backup: {when}, {'pushed to origin/' + config.settings.branch if last.get('pushed') else last.get('why') or 'local journal branch only'}"
    return f"backup FAILED {when}: {last.get('error', '')[:120]}  ({_cmd('sync')} to retry)"


@journal_app.command("executions")
def cmd_executions(
    limit: int = LimitOpt,
    since: Optional[str] = SinceOpt,
    actor: Optional[str] = ActorOpt,
    where: Optional[str] = WhereFilter,
    writes: bool = typer.Option(False, "--writes", help="Only runs that changed something."),
    grep: List[str] = GrepOpt,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Every run, reads included: the command, who ran it, what it resolved, exit code, seconds."""
    rows = journal.read(journal.COMMANDS)
    if writes:
        rows = [r for r in rows if r.get("writes")]
    rows = _filter(rows, limit, since, actor, where, grep)
    if json_output:
        print_json(rows)
        return
    if not rows:
        console.print(f"[yellow]No runs recorded{' matching' if any((since, actor, where, writes, grep)) else ''}. Journal: {_where_written()}[/yellow]")
        return
    table = Table(show_lines=False)
    for col in ("when (UTC)", "run", "actor", "where", "command", "exit", "s"):
        table.add_column(col, overflow="fold")
    for r in rows:
        code = r.get("exit", "")
        style = "green" if code == 0 else "red"
        table.add_row(r.get("ts", ""), str(r.get("invocation", ""))[:6], str(r.get("actor", "")), journal.target_of(r),
                      _ran(r.get("argv", [])) + (f"\n[red]{r['error']}[/red]" if r.get("error") else ""),
                      f"[{style}]{code}[/{style}]", str(r.get("seconds", "")))
    console.print(table)
    console.print(f"[dim]{_cmd('show')} RUN: one run with the writes it made[/dim]")


@journal_app.command("show")
def cmd_show(
    run_id: str = typer.Argument(..., help="Run id or its start (the `run` column of executions / changes)."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """One run: the command, who ran it, where, exit code, every write it made (before -> after) and the runs it
    started (task steps). Exit 1 when no run has that id."""
    try:
        found = journal.invocation(run_id)
    except journal.AmbiguousId as exc:
        raise typer.BadParameter(str(exc), param_hint="RUN_ID") from exc
    if found is None:
        if json_output:
            print_json(None)
        else:
            console.print(f"[red]No run {run_id!r} in {_where_written()}[/red] ({_cmd('executions')} -g WORDS to find it)")
        raise typer.Exit(1)
    if json_output:
        print_json(found)
        return
    _print_run(found)


def _print_run(found: dict) -> None:
    from rich.markup import escape

    run = found["run"] or {}
    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim")
    head.add_column(overflow="fold")
    code = run.get("exit", "?")
    rows = [
        ("run", found["id"] + (f"  (started by run {run['parent']})" if run.get("parent") else "")),
        ("command", escape(_ran(run.get("argv", []))) if run else "[yellow](no run line: it did not finish)[/yellow]"),
        ("ended (UTC)", f"{run.get('ts', '')}  (took {run.get('seconds', '?')} s)"),
        ("who", f"{run.get('actor', '')} ({run.get('by', '')})"),
        ("where", journal.target_of(run) or "-"),
        ("cwd", run.get("cwd", "")),
        ("exit", f"[{'green' if code == 0 else 'red'}]{code}[/]"),
    ]
    if run.get("error"):
        rows.append(("error", f"[red]{escape(run['error'])}[/red]"))
    if run.get("logs"):
        rows.append(("logs", "\n".join(run["logs"])))
    if run.get("run"):
        rows.append(("CI run", run["run"]))
    for k, v in rows:
        head.add_row(k, str(v))
    console.print(head)
    if found["writes"]:
        table = Table(title=f"{len(found['writes'])} write(s)", title_justify="left", show_lines=False)
        for col in ("when (UTC)", "where", "what", "status"):
            table.add_column(col, overflow="fold")
        for w in found["writes"]:
            table.add_row(w.get("ts", ""), journal.target_of(w), escape(journal.summary(w)), str(w.get("status", "")))
        console.print(table)
    else:
        console.print("[dim]no writes[/dim]")
    for child in found["children"]:
        console.print(f"  [dim]started[/dim] {child.get('invocation', '')}  exit {child.get('exit', '')}  "
                      f"{escape(_ran(child.get('argv', [])))}")
    console.print("[dim]--json for every field (params, before/after)[/dim]")


def _running(grep: Optional[List[str]] = None) -> list[dict]:
    from opsjournal import running as journal_running

    rows = journal_running.listing(journal.journal_dir(), skip=journal.current_run())
    return [r for r in rows if journal.grep_match({**r, "_stream": journal.COMMANDS}, grep or [])]


def _since(ts: str) -> str:
    import datetime

    try:
        started = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return ""
    secs = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


@journal_app.command("running")
def cmd_running(
    grep: List[str] = GrepOpt,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """The commands running now on this machine, in any shell or agent session: run id, who, where, since when,
    the command, its log file. Exit 1 when none (matches).

    Follow one with `opsjournal follow RUN`. A run killed without ending (SIGKILL) is dropped here."""
    from rich.markup import escape

    rows = _running(grep)
    if json_output:
        print_json(rows)
    elif not rows:
        console.print(f"[yellow]No command running{' matching' if grep else ''}. Journal: {_where_written()}[/yellow]")
    else:
        table = Table(show_lines=False)
        for col in ("run", "started (UTC)", "for", "actor", "where", "command", "log"):
            table.add_column(col, overflow="fold")
        for r in rows:
            who = str(r.get("actor", "")) + (f"\n{r['session']}" if r.get("session") else "")
            where = journal.target_of(r) or "/".join(x for x in (r.get("branch"), r.get("ticket")) if x) or r.get("cwd", "")
            table.add_row(str(r.get("invocation", ""))[:6], r.get("ts", ""), _since(r.get("ts", "")), escape(who), escape(where),
                          escape(_ran(r.get("argv", []))), escape((r.get("logs") or ["-"])[-1]))
        console.print(table)
        console.print(f"[dim]{_cmd('follow')} RUN (or --last): print its log as it grows, exit with its exit code[/dim]")
    if not rows:
        raise typer.Exit(1)


@journal_app.command("follow")
def cmd_follow(
    run_id: Optional[str] = typer.Argument(None, help="Run id or its start (`opsjournal running`). None: the only running command, or --last / -g."),
    last: bool = typer.Option(False, "--last", "-l", help="The command started last."),
    grep: List[str] = typer.Option([], "--grep", "-g", help="The newest running command matching: all words of one -g, any order (-g 'deploy api')."),
    timeout: Optional[float] = typer.Option(None, "--timeout", help="Give up after this many seconds (exit 124)."),
) -> None:
    """Print a running command's log as it grows (the files it lists in $<PREFIX>_RUN_LOGS) and exit with that
    command's exit code when it ends: 124 after --timeout, 1 when it died without ending (killed), 2 when there is
    no such run. An agent runs it in the background and acts on the end.

    A run that already ended: prints its logs and exits with its code at once. A command that lists no log: it only
    waits for the end."""

    from opsjournal import running as journal_running

    folder = journal.journal_dir()
    if folder is None:
        raise typer.BadParameter(f"the journal is {_where_written()}")
    rows = _running(grep)
    if run_id:
        rows = [r for r in rows if str(r.get("invocation", "")).startswith(run_id)]
        if not rows:  # ended already: its logs and its code
            try:
                found = journal.invocation(run_id)
            except journal.AmbiguousId as exc:
                raise typer.BadParameter(str(exc), param_hint="RUN_ID") from exc
            if found is None or not found["run"]:
                console.print(f"[red]No run {run_id!r} running or ended in {_where_written()}[/red] ({_cmd('running')})")
                raise typer.Exit(2)
            rows = [{**found["run"], "pid": 0}]
    if not rows:
        print(f"No command running{' matching' if grep else ''} ({_cmd('running')})", file=sys.stderr)
        raise typer.Exit(2)
    if len(rows) > 1 and not (last or grep or run_id):
        listed = "; ".join(f"{r.get('invocation', '')[:6]} {_ran(r.get('argv', []))}" for r in rows)
        raise typer.BadParameter(f"{len(rows)} commands are running ({listed}): give a RUN id, --last or -g WORDS",
                                 param_hint="RUN_ID")
    run = rows[-1]
    print(f"[follow] run {run.get('invocation', '')}: {_ran(run.get('argv', []))} "
          f"({run.get('actor', '')}{', ' + run['session'] if run.get('session') else ''}; started {run.get('ts', '')})", file=sys.stderr)
    if run.get("pid") and not journal_running.logs_of(folder, run["invocation"]):
        print("[follow] no log yet; waiting for it to end", file=sys.stderr)

    def emit(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    code = journal_running.follow(folder, run, emit, timeout=timeout)
    if code is None:
        print(f"[follow] run {run['invocation']} ended without an end line (killed?); exit 1", file=sys.stderr)
        raise typer.Exit(1)
    print(f"[follow] run {run['invocation']} " + ("still running after --timeout; exit 124" if code == 124 and run.get("pid")
          and journal_running.end_line(folder, run["invocation"]) is None else f"ended: exit {code}"), file=sys.stderr)
    raise typer.Exit(code)


@journal_app.command("note")
def cmd_note(
    text: str = typer.Argument(..., help="What was done and why, e.g. 'bumped the api image to 3.9.0 by kubectl edit'."),
    command: List[str] = typer.Option([], "--cmd", help="A command that was run by hand (repeatable)."),
    env: Optional[str] = typer.Option(None, "--where", "-w", help="Where (an environment, region, host)."),
    cluster: Optional[str] = typer.Option(None, "--cluster", "-c"),
    ticket: Optional[str] = typer.Option(None, "--ticket", "-t", help="A ticket, e.g. OPS-123."),
) -> None:
    """Record a change made outside the CLI (a raw command, a UI, a teammate's change), so `changes` has it."""
    if journal.journal_dir() is None:
        raise typer.BadParameter(f"the journal is {_where_written()}")
    journal.note(text, commands=command, env=env, cluster=cluster, ticket=ticket)
    console.print(f"[green]Noted[/green] ({journal.actor()}) in {journal.journal_dir() / 'actions.jsonl'}")


@journal_app.command("prompt")
def cmd_prompt(
    tool: Optional[str] = typer.Option(None, "--tool", help="The agent tool whose hook this is (default: the one this shell belongs to)."),
) -> None:
    """For an agent tool's prompt hook: record the message the user sent (the hook's JSON on stdin) in prompts.jsonl.

    Always exits 0 and prints nothing, so it never stops a prompt. Optional: a tool without prompt hooks just has no
    prompts.jsonl lines. The hook runs `opsjournal prompt` with a JSON object on stdin holding `prompt` and,
    if the tool has them, `session_id` and `cwd`."""
    from opsjournal import prompts

    prompts.record(sys.stdin.read() if not sys.stdin.isatty() else "", tool)


@journal_app.command("prompts")
def cmd_prompts(
    limit: int = LimitOpt,
    since: Optional[str] = SinceOpt,
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="Only messages sent in this repo."),
    ticket: Optional[str] = typer.Option(None, "--ticket", "-t", help="Only messages sent on this ticket's branch (OPS-123)."),
    grep: List[str] = GrepOpt,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """The messages you sent to agents, oldest to newest: when, repo / branch, session, the text.

    Recorded by `opsjournal prompt` from a prompt hook; exit 1 when nothing matches."""
    from rich.markup import escape

    from opsjournal import prompts

    rows = journal.read(prompts.STREAM)
    if repo:
        rows = [r for r in rows if r.get("repo") == repo]
    if ticket:
        rows = [r for r in rows if str(r.get("ticket", "")).upper() == ticket.upper()]
    if grep:  # grep_match reads `note`: give it the message and where it was sent
        rows = [r for r in rows if journal.grep_match({**r, "note": f"{r.get('prompt', '')} {r.get('repo', '')} {r.get('branch', '')}"}, grep)]
    rows = _filter(rows, limit, since, None, None)
    if json_output:
        print_json(rows)
    elif not rows:
        console.print(f"[yellow]No prompts recorded{' matching' if any((since, repo, ticket, grep)) else ''}. Journal: "
                      f"{_where_written()} (a prompt hook runs `{_cmd('prompt')}`: see its --help)[/yellow]")
    else:
        for r in rows:
            where = "/".join(x for x in (r.get("repo"), r.get("branch")) if x) or r.get("cwd", "")
            console.print(f"[bold]{r.get('ts', '')}[/bold]  {escape(where)}  [dim]{escape(str(r.get('session', '')))}[/dim]", highlight=False)
            console.print(escape(r.get("prompt", "")), highlight=False, soft_wrap=True)
            console.print()
    if not rows:
        raise typer.Exit(1)


@journal_app.command("sync")
def cmd_sync(
    no_push: bool = typer.Option(False, "--no-push", help="Commit to the local journal branch only."),
    quiet: bool = typer.Option(False, "--quiet", help="Print nothing on success (the background sync)."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Back the journal up: commit this machine's journal files to the `journal` branch of the git repo the journal
    folder is in, and push it.

    A wrapper's runs do it by themselves in the background, at most every 15 minutes (<PREFIX>_JOURNAL_SYNC=off
    stops that). The checkout is never touched: the branch is written with git plumbing, one folder per
    machine (machines/<user>@<host>/), so machines and the runner never conflict."""
    from opsjournal import sync as journal_sync

    folder = journal.journal_dir()
    if folder is None:
        raise typer.BadParameter(f"the journal is {_where_written()}")
    try:
        result = journal_sync.sync(folder, push=not no_push)
    except (journal_sync.SyncError, OSError, subprocess.TimeoutExpired) as exc:
        journal_sync.record({"ok": False, "error": str(exc)})
        console.print(f"[red]journal sync failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    journal_sync.record({"ok": True, **result})
    if json_output:
        print_json(result)
    elif not quiet:
        where = f"pushed to origin/{config.settings.branch}" if result.get("pushed") else result.get("why") or ("not pushed" + (" (--no-push)" if no_push else ""))
        console.print(f"[green]{result['status']}[/green], {where}: machines/{result['machine']}/ in {result.get('repo', '')}")
        if result.get("merged"):
            console.print(f"[yellow]{', '.join(result['merged'])}: shorter here than in the backup; kept the lines of both[/yellow]")



def main() -> None:
    journal_app()
