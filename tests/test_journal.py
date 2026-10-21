"""The journal: every run in commands.jsonl, every write in actions.jsonl, who did it (user / agent / runner)."""
import json
import os

import pytest
import typer
from typer.testing import CliRunner

from opsjournal import config, journal
from opsjournal.cli import journal_app


def lines(folder, stream):
    path = folder / f"{stream}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_actor_user_agent_runner_or_explicit(jdir, monkeypatch):
    assert journal.actor() == "user"
    monkeypatch.setenv("CLAUDECODE", "1")
    assert journal.actor() == "agent:claude-code"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert journal.actor() == "runner"
    monkeypatch.setenv("AGENTS_ACTOR", "agent:aider")
    assert journal.actor() == "agent:aider"


def test_secrets_are_redacted():
    assert journal.redact_argv(["auth", "--token", "abc", "--api-key=xyz", "-w", "prod"]) == [
        "auth", "--token", "***", "--api-key=***", "-w", "prod"]
    assert journal.redact_argv(["run", "--var", "SITE=a", "--var", "API_TOKEN=abc"]) == [
        "run", "--var", "SITE=a", "--var", "API_TOKEN=***"]
    assert journal._params({"api_token": "abc", "env": "prod", "yes": False, "dry_run": False}) == {"api_token": "***", "env": "prod"}
    assert journal._params({"values": ["A=1", "Always"]}, mask=("values",)) == {"values": ["A=***", "Always"]}


def test_env_value_argv_hides_every_value_of_those_commands():
    config.configure(env_value_argv=lambda argv: argv[:2] == ["set", "env"])
    assert journal.redact_argv(["set", "env", "DB_URL=postgres://u:p@h", "DEBUG-"]) == ["set", "env", "DB_URL=***", "DEBUG-"]
    assert journal.redact_argv(["run", "DB_URL=x"]) == ["run", "DB_URL=x"]


def tiny_app():
    app = typer.Typer()

    @app.command("restart")
    @journal.journaled
    def restart(deployment: str, env: str = "prod", fail: bool = False, ask: bool = False, dry_run: bool = typer.Option(False, "--dry-run")):
        journal.target(env=env, cluster="ai")
        journal.add(image_before="v1")
        if ask:
            typer.confirm("go?", abort=True)
        raise typer.Exit(3 if fail else 0)

    @app.command("other")
    def other():
        pass

    return app


@pytest.mark.parametrize("args, status, code", [
    (["restart", "api"], "ok", 0),
    (["restart", "api", "--fail"], "failed", 3),
    (["restart", "api", "--ask"], "declined", 1),
])
def test_a_write_is_recorded_with_its_outcome(jdir, args, status, code):
    result = CliRunner().invoke(tiny_app(), args, input="n\n")
    assert result.exit_code == code
    [entry] = lines(jdir, journal.ACTIONS)
    assert entry["status"] == status and entry["actor"] == "user"
    assert entry["params"]["deployment"] == "api" and entry["cluster"] == "ai" and entry["image_before"] == "v1"
    assert journal.target_of(entry) == "prod/ai"


def test_dry_run_is_not_a_write(jdir):
    CliRunner().invoke(tiny_app(), ["restart", "api", "--dry-run"])
    assert lines(jdir, journal.ACTIONS) == []


def test_journal_off_writes_nothing(jdir, monkeypatch):
    monkeypatch.setenv("OPSJOURNAL_DIR", "off")
    CliRunner().invoke(tiny_app(), ["restart", "api"])
    assert not jdir.exists()


def test_every_run_is_a_line_in_commands(jdir, monkeypatch):
    monkeypatch.setattr("sys.argv", ["mycli", "restart", "api", "--fail"])
    app = tiny_app()
    with pytest.raises(SystemExit):
        journal.run_main(lambda: app(["restart", "api", "--fail"]))
    [run] = lines(jdir, journal.COMMANDS)
    assert run["argv"] == ["restart", "api", "--fail"] and run["exit"] == 3
    assert run["env"] == "prod" and run["cluster"] == "ai" and run["writes"] == 1
    [action] = lines(jdir, journal.ACTIONS)
    assert action["invocation"] == run["invocation"]


def test_a_wrappers_own_stream_counts_as_a_write_once_named(jdir):
    config.configure(streams=("db", "actions"), summarize=lambda r: f"db {r['key']}" if r.get("_stream") == "db" else None)
    journal.begin(["db", "put"])
    journal.append("db", {"key": "a.b"})
    journal.append("other", {"key": "x"})
    journal.finish(0)
    [run] = lines(jdir, journal.COMMANDS)
    assert run["writes"] == 1
    assert [journal.summary(r) for r in journal.read("db")] == ["db a.b"]


def test_help_and_completion_are_not_recorded(jdir, monkeypatch):
    journal.begin(["x", "--help"])
    journal.finish(0)
    monkeypatch.setenv("_OPSJOURNAL_COMPLETE", "complete_zsh")
    journal.begin(["x", "restart"])
    journal.finish(0)
    assert lines(jdir, journal.COMMANDS) == []


def test_since():
    assert journal.since("2026-10-10") == "2026-10-10T00:00:00Z"
    assert journal.since("2h") < journal.since("1h")
    with pytest.raises(ValueError):
        journal.since("yesterday")


def test_parent_is_the_direct_parent_and_the_env_is_put_back(jdir, monkeypatch):
    monkeypatch.delenv("OPSJOURNAL_PARENT", raising=False)
    journal.begin(["task", "run", "x"])
    outer = journal._run["id"]
    assert os.environ["OPSJOURNAL_PARENT"] == outer and "parent" not in journal.who()
    journal.finish(0)
    assert "OPSJOURNAL_PARENT" not in os.environ
    monkeypatch.setenv("OPSJOURNAL_PARENT", "abc")
    journal.begin(["restart", "x"])
    assert journal.who()["parent"] == "abc"
    journal.finish(0)
    assert os.environ["OPSJOURNAL_PARENT"] == "abc"


def test_a_crash_is_recorded_with_where_it_happened(jdir, monkeypatch):
    config.configure(package="tests")

    def boom():
        raise RuntimeError("half-written module")

    monkeypatch.setattr("sys.argv", ["mycli", "status"])
    with pytest.raises(RuntimeError):
        journal.run_main(boom)
    [run] = lines(jdir, journal.COMMANDS)
    assert run["exit"] == 1 and run["error"].startswith("RuntimeError: half-written module")
    assert "test_journal.py" in run["error"]


def test_a_chosen_exit_code_is_not_an_error(jdir, monkeypatch):
    def chosen():
        try:
            raise typer.Exit(2)
        except typer.Exit:
            raise SystemExit(2)

    monkeypatch.setattr("sys.argv", ["mycli", "x"])
    with pytest.raises(SystemExit):
        journal.run_main(chosen)
    [run] = lines(jdir, journal.COMMANDS)
    assert run["exit"] == 2 and "error" not in run


def test_note_changes_and_show_from_the_cli(jdir):
    runner = CliRunner()
    assert runner.invoke(journal_app, ["changes"]).exit_code == 0
    result = runner.invoke(journal_app, ["note", "scaled api to 3 by hand", "-w", "prod", "--cmd", "kubectl scale"])
    assert result.exit_code == 0, result.output
    rows = json.loads(runner.invoke(journal_app, ["changes", "--json"]).output)
    assert [r["note"] for r in rows] == ["scaled api to 3 by hand"] and rows[0]["env"] == "prod"
    assert "note: scaled api to 3" in runner.invoke(journal_app, ["changes", "-w", "prod"]).output
    assert json.loads(runner.invoke(journal_app, ["changes", "-w", "staging", "--json"]).output) == []
    journal.begin(["deploy", "api"])
    journal.note("inside a run")
    journal.finish(0)
    [run] = lines(jdir, journal.COMMANDS)
    shown = runner.invoke(journal_app, ["show", run["invocation"][:5]])
    assert shown.exit_code == 0 and "inside a run" in shown.output and "opsjournal deploy api" in shown.output
    assert runner.invoke(journal_app, ["show", "zzzz"]).exit_code == 1


def test_grep_all_words_any_order_and_exclusions():
    row = {"_stream": "commands", "argv": ["deploy", "api", "prod"], "actor": "user"}
    assert journal.grep_match(row, ["prod deploy"]) and not journal.grep_match(row, ["deploy -prod"])
    assert journal.grep_match(row, ["nope", "api"]) and journal.grep_match(row, [])
