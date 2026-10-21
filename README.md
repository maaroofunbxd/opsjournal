# opsjournal

An audit log for a command-line tool that changes live systems: every run, every write it made, and who did it:
you (`user`), a CI runner (`runner`) or an AI agent (`agent:<tool>`). Plain JSONL files a person or any agent can
read; the commands running right now are visible and followable from another shell; the files are backed up to a
git branch. It is the part worth having anywhere AI agents are allowed to touch production.

```bash
pip install "agentboard @ git+https://github.com/maaroofunbxd/agentboard@v0.4.0" \
            "opsjournal @ git+https://github.com/maaroofunbxd/opsjournal@v0.1.0"
opsjournal note "scaled api to 3 by hand" -w prod --cmd "kubectl scale deploy/api --replicas 3"
opsjournal changes --since 1d --actor agent   # what changed, by agents, in the last day
opsjournal executions -g "deploy api"         # every run, reads too, with exit code and run id
opsjournal show 3f9c2a                        # one run: its command, exit, its writes, the runs it started
opsjournal running                            # what's running now, in any shell or agent session
opsjournal follow --last                      # its log as it grows; exits with its exit code
opsjournal sync                               # back the journal up to the `journal` branch now
```

On its own, the journal is the folder `$OPSJOURNAL_DIR`, else `~/.opsjournal` (`OPSJOURNAL_DIR=off` turns it off).
Who is an agent comes from [agentboard](https://github.com/maaroofunbxd/agentboard)'s identity: the variables an agent
tool sets, or `AGENTS_ACTOR=agent:<name>`.

## Recording your own CLI

```python
import opsjournal
from opsjournal import journaled

opsjournal.configure(folder=lambda: my_data_dir() / "journal", env_prefix="MYCLI", cli="mycli",
                     journal_argv=("journal",), package="mycli")


@app.command("restart")
@journaled                       # one line in actions.jsonl: params, target, outcome; skipped for --dry-run
def restart(deployment: str, env: str, dry_run: bool = False):
    opsjournal.target(env=env, cluster="eu")   # where it works: any names
    opsjournal.add(image_before="v1")          # more fields for this write
    ...


def main():
    opsjournal.run_main(app)     # one line in commands.jsonl per run, exit code and crash included
```

A write is recorded also when it fails (`failed`, `error`) or the user says no at the question (`declined`).
`@journaled(when=lambda kw: ..., mask=("values",))`: skip calls that write nothing; record KEY=VALUE items of a list
parameter as KEY=***. Something done outside the CLI: `opsjournal.note(text, commands=[...], **where)`.
A writer of its own (a config store's old -> new values, say) calls `opsjournal.append("mystream", entry)` and names
the stream in `streams`.

## The files

In the journal folder; every line starts with `ts`, `by` (the OS or GitHub user), `actor`, `session` (agentboard's
session name, for an agent), `invocation` (the run id), `parent` (the run that started this one), `run` (the CI run's
URL) and `source` (the command line, secrets as `***`).

| File | One line per | Fields |
|---|---|---|
| `commands.jsonl` | run | `argv`, `cwd`, `tty`, `exit`, `seconds`, the target fields, `writes` (how many), `error` (a crash: the exception and the innermost frame of `package`), `logs` |
| `actions.jsonl` | write or note | `action` (the command path), `status` (`ok` / `failed` / `declined` / `interrupted` / `error`), `params`, the target fields, `exit`, `error`, `seconds`; a note: `note`, `commands` |
| `<stream>.jsonl` | a wrapper's own writes | whatever it appends |
| `prompts.jsonl` | message sent to an agent | `tool`, `repo`, `branch`, `ticket`, `session`, `cwd`, `prompt` (`opsjournal prompt` from a prompt hook, JSON on stdin) |
| `running/<id>.json` | run going on now | `pid`, `argv`, `cwd`, `branch`, `ticket`, who; removed when it ends (a killed run's file is dropped by the next listing) |
| `running/<id>.logs` | — | the log files the run writes, one path a line: anything it starts appends to `$<PREFIX>_RUN_LOGS`; `follow` tails them |

Secrets are hidden in `argv` and `params`: the value after a flag named like a secret (`--token X`), a
`KEY=VALUE` whose KEY is (`DB_PASSWORD=x`), and every value of the argv shapes `env_value_argv` names.

**Backup** (`opsjournal sync`, and by itself in the background after a wrapped run, at most every 15 minutes;
`<PREFIX>_JOURNAL_SYNC=off` stops that): the journal folder must sit in a git repo. Each machine owns
`machines/<user>@<host>/` on the `journal` branch; the commit is built with git plumbing (the checkout is never
touched) and pushed without pre-push hooks. A local file shorter than the backup (a fresh clone) is merged, never
replaces it. A lost machine's journal: `git show origin/journal:machines/<user>@<host>/actions.jsonl`.

## Settings

`opsjournal.configure(**changes)`, once at import, before anything is recorded:

| Setting | Default | What |
|---|---|---|
| `folder` | `$<PREFIX>_DIR`, else `~/.opsjournal` | `() -> Path or None` (None: off) |
| `env_prefix` | `OPSJOURNAL` | `$<PREFIX>_PARENT`, `_RUN_LOGS`, `_JOURNAL_SYNC`, `_<PREFIX>_COMPLETE` |
| `cli` | `opsjournal` | the CLI's name in messages, and how a running process is recognised |
| `journal_argv` | `()` | where the journal commands sit in the CLI's argv (`("journal",)` for `mycli journal ...`) |
| `package` | `opsjournal` | a crash's `error` names the innermost frame from a path containing this |
| `env_value_argv` | none | `argv -> bool`: commands whose every `KEY=VALUE` is a secret |
| `streams` | `("actions",)` | the files with changes in them |
| `summarize` | none | `row -> str or None`: one line for a row of your own streams |
| `bare_params`, `hidden_params` | | how the generic summary shows `params` |
| `target_keys`, `where` | `env`, `cluster`, `host` | the `where` column |
| `branch`, `sync_author`, `state_file`, `sync_argv`, `extra_backup` | `journal`, … | the backup |
| `off_reason` | | said when `folder()` is None |

## Rules for AI agents on live systems

The journal records what happened; these rules are why it exists. Put them in your agents' instructions.

- **Say what you touched:** every report names the environment, cluster and service your commands ran against.
- **Writes: preview, then ask.** `--dry-run` / `plan` first; no `--yes` unless the user said so for that run.
- **An approval covers the run it was given for.** In production, ask before every write, even after an earlier yes.
- **Try it outside production first**, and still ask before a write to a shared environment.
- **An agent can't start what a person can't stop:** long jobs (data loads, pipelines) are started by a person.
- **Check before you write:** look at what's there first; if it is, leave it and say so.
- **A change made outside the tool is noted right after:** `opsjournal note "what and why" --cmd "the command"`.
- **A check gates only if it can fail:** before using a command as a gate, show it exits non-zero when broken.
- **No credentials in commits.**

## Development

```bash
uv sync && uv run ruff check src tests && uv run pytest -q
```

MIT licensed.
