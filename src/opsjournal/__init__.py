"""opsjournal: an audit log of every run of a CLI and every write it made to a live system, and who did it."""
from opsjournal.config import configure, settings  # noqa: F401
from opsjournal.journal import (  # noqa: F401
    ACTIONS,
    COMMANDS,
    AmbiguousId,
    actor,
    add,
    append,
    begin,
    count_write,
    current_run,
    finish,
    grep_match,
    invocation,
    journal_dir,
    journaled,
    mask_pair,
    note,
    read,
    redact_argv,
    run_main,
    searchable,
    session_id,
    since,
    source,
    streams,
    summary,
    target,
    target_of,
    who,
)

__version__ = "0.1.0"
