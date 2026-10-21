"""prompts.jsonl: every message the user sends to an agent, one line each, so they can feed documentation later.

Written by `opsjournal prompt`, which an agent tool's prompt hook runs (if the tool has hooks; optional) with
the hook's JSON on stdin: {"prompt", "session_id", "cwd", ...}.
Each line: ts, by, tool, repo (from the hook's cwd, by its main checkout's name), branch, ticket (CW-123 in the
branch, if any), session (the name `agents whoami` prints), cwd, prompt. Read back with `opsjournal prompts`;
backed up with the other journal files (sync.py copies every *.jsonl).

A hook runs before every message, so this must be fast and must never stop the prompt: a wrapper answers it before
importing the rest of its CLI, and `record` swallows every error (the hook always exits 0).
"""
from __future__ import annotations

import datetime
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

STREAM = "prompts"
TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]+-\d+)(?![0-9])")
MAX_CHARS = 50_000  # a pasted log can be huge; the head is what documents the request


def git_place(cwd: str) -> tuple[Optional[str], Optional[str]]:
    """(repo, branch) for a folder: the repo by its main checkout's name, the same in every worktree (agentboard.identity.here)."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir",
                              "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=2).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):
        return None, None
    if len(out) < 2 or not out[0]:
        return None, None
    common = Path(out[0])
    repo = (common.parent if common.name == ".git" else common).name.removesuffix(".git")
    branch = out[1].strip()
    return repo, (branch if branch and branch != "HEAD" else None)


def ticket_of(branch: Optional[str]) -> Optional[str]:
    match = TICKET.search(branch or "")
    return match.group(1) if match else None


def tool_name(tool: Optional[str] = None) -> str:
    """The tool a hook runs in: --tool when given, else the agent this shell belongs to, else `agent`."""
    from agentboard import identity

    if tool:
        return tool
    who = identity.actor()
    return who.removeprefix("agent:") if who.startswith("agent:") else "agent"


def entry(hook: dict[str, Any], tool: Optional[str] = None) -> Optional[dict]:
    """The line for one hook payload, None when it carries no prompt."""
    from agentboard import identity
    from agentboard import tools as agent_tools

    prompt = hook.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    cwd = str(hook.get("cwd") or os.getcwd())
    repo, branch = git_place(cwd)
    sid = str(hook.get("session_id") or "")
    tool = tool_name(tool)
    var = getattr(agent_tools.named(tool), "session_env", "")
    if sid and var and not os.environ.get(var):
        os.environ[var] = sid  # so the name matches what whoami prints in that session
    line: dict[str, Any] = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by": os.environ.get("GITHUB_ACTOR") or getpass.getuser(),
        "tool": tool, "repo": repo, "branch": branch, "ticket": ticket_of(branch),
        "session": identity.session_name(repo), "cwd": cwd, "prompt": prompt[:MAX_CHARS],
    }
    if len(prompt) > MAX_CHARS:
        line["truncated"] = len(prompt)
    return {k: v for k, v in line.items() if v is not None}


def path() -> Optional[Path]:
    from opsjournal import config

    found = config.settings.folder()
    return found / f"{STREAM}.jsonl" if found else None


def record(raw: str, tool: Optional[str] = None) -> Optional[dict]:
    """Append the hook payload's prompt to prompts.jsonl. Never raises: a broken journal must not block a message
    (the reason goes to stderr, which the hook shows only in its debug output)."""
    try:
        target = path()
        line = entry(json.loads(raw), tool) if target and raw.strip() else None
        if line is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        return line
    except Exception as exc:  # noqa: BLE001 - anything at all: the prompt goes through
        print(f"journal prompt: not recorded ({exc.__class__.__name__}: {exc})", file=sys.stderr)
        return None
