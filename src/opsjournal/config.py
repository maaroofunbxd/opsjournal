"""Where the journal lives and what it calls itself: the settings a program using opsjournal gives it.

Defaults, for the journal on its own: the folder $OPSJOURNAL_DIR, else ~/.opsjournal (OPSJOURNAL_DIR=off turns it
off); variables named OPSJOURNAL_*; the command `opsjournal`. A program wraps it by calling `configure(...)` once at
import, before anything is recorded (for example to keep the journal in its own data folder, name the variables
<ITS PREFIX>_* and say how its own rows read).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

OFF = ("0", "off", "none", "false")


def _default_folder() -> Optional[Path]:
    value = os.environ.get(f"{settings.env_prefix}_DIR", "").strip()
    if value.lower() in OFF:
        return None
    return Path(value or "~/.opsjournal").expanduser()


def _no_argv(argv: list) -> bool:
    return False


def _no_summary(row: dict) -> Optional[str]:
    return None


def _default_where(row: dict) -> str:
    return "/".join(str(row[k]) for k in settings.target_keys if row.get(k))


def _no_extra_files(repo: Path) -> list:
    return []


@dataclass(frozen=True)
class Settings:
    # the folder holding the *.jsonl files; None = the journal is off
    folder: Callable[[], Optional[Path]] = _default_folder
    env_prefix: str = "OPSJOURNAL"  # $<prefix>_PARENT, _RUN_LOGS, _JOURNAL_SYNC, _<prefix>_COMPLETE
    cli: str = "opsjournal"  # the wrapped CLI's name: in messages, and how a running process is recognised
    # where the journal commands sit in the wrapped CLI's argv (("journal",) for `unbxd journal ...`)
    journal_argv: tuple = ()
    package: str = "opsjournal"  # a crash's `error` names the innermost frame from a file with this in its path
    # argv shapes whose every KEY=VALUE is a secret, whatever the KEY (`unbxd k8s set DEPLOY env K=V`)
    env_value_argv: Callable[[list], bool] = _no_argv
    # the files with changes in them (commands.jsonl is every run): a wrapper adds its own writers' files
    streams: tuple = ("actions",)
    # one line for a row of the wrapper's own streams; None = the generic summary
    summarize: Callable[[dict], Optional[str]] = _no_summary
    bare_params: tuple = ("args", "command", "what", "values", "key", "ticket")  # shown as the value alone
    hidden_params: tuple = ("yes", "timeout")  # left out of a summary
    target_keys: tuple = ("env", "cluster", "host")  # target(**fields) names `where` shows, in this order
    where: Callable[[dict], str] = _default_where  # the "where" column of a row
    # the git branch the backup goes to, who commits, and the background sync's last attempt
    branch: str = "journal"
    sync_author: tuple = ("opsjournal", "opsjournal@localhost")
    state_file: Callable[[], Path] = lambda: Path("~/.opsjournal-sync.json").expanduser()
    sync_argv: Optional[list] = None  # the background sync's command; None = python -m opsjournal sync --quiet
    extra_backup: Callable[[Path], list] = _no_extra_files  # (path in the backup, file) pairs from the repo
    off_reason: str = "the journal is off (OPSJOURNAL_DIR=off)"  # said when folder() is None

    def env(self, name: str) -> str:
        return f"{self.env_prefix}_{name}"

    def command(self, *words: str) -> str:
        """How messages name a journal command: `opsjournal changes`, `unbxd journal changes`."""
        return " ".join((self.cli, *self.journal_argv, *words))


settings = Settings()


def configure(**changes) -> Settings:
    """Replace some settings (the rest stay); returns the new ones."""
    global settings
    settings = replace(settings, **changes)
    return settings
