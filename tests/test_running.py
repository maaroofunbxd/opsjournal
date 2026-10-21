"""Running runs (running/<id>.json), `opsjournal running` and `opsjournal follow`."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opsjournal import config, journal
from opsjournal import running as journal_running
from opsjournal.cli import journal_app as app


def lines(folder, stream):
    path = folder / f"{stream}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_a_run_is_listed_while_it_runs_then_ends_with_its_logs(jdir, tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["mycli", "k8s", "run", "-f", "x.txt", "--dry-run"])
    seen = {}

    def command():
        seen["running"] = journal_running.listing(jdir)
        journal.target(region="pilot", cluster="ai")
        seen["after_target"] = journal_running.listing(jdir)
        log = tmp_path / "20261013-154825.log"
        log.write_text("CMD[1]: kubectl get pods\n")
        with open(os.environ[journal_running.logs_env()], "a") as fh:  # what a script the run starts does
            fh.write(f"{log}\n")
        raise SystemExit(3)

    with pytest.raises(SystemExit):
        journal.run_main(command)
    [run] = seen["running"]
    assert run["argv"] == ["k8s", "run", "-f", "x.txt", "--dry-run"] and run["pid"] == os.getpid() and run["actor"] == "user"
    assert "region" not in run and seen["after_target"][0]["region"] == "pilot"
    [end] = lines(jdir, journal.COMMANDS)
    assert end["invocation"] == run["invocation"] and end["exit"] == 3
    assert end["logs"] == [str(tmp_path / "20261013-154825.log")]
    assert journal_running.listing(jdir) == [] and not list((jdir / "running").iterdir())
    assert journal_running.logs_env() not in os.environ


def test_watching_commands_are_not_listed(jdir, monkeypatch):
    journal.begin(["follow", "--last"])
    assert journal_running.listing(jdir) == []
    journal.finish(0)
    config.configure(journal_argv=("journal",))
    journal.begin(["journal", "running"])
    assert journal_running.listing(jdir) == []
    journal.finish(0)


def test_a_killed_run_drops_off(jdir):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (jdir / "running").mkdir(parents=True)
        (jdir / "running" / "aaa111.json").write_text(json.dumps({"invocation": "aaa111", "pid": proc.pid, "ts": "2026-10-13T10:00:00Z"}))
        (jdir / "running" / "bbb222.json").write_text(json.dumps({"invocation": "bbb222", "pid": 999_999, "ts": "2026-10-13T09:00:00Z"}))
        assert [r["invocation"] for r in journal_running.listing(jdir)] == ["aaa111"]
        assert not (jdir / "running" / "bbb222.json").exists()
        proc.kill()
        proc.wait()
        assert journal_running.listing(jdir) == []
    finally:
        proc.kill()


def _fake_run(jdir, log: Path, run_id="ccc333"):
    (jdir / "running").mkdir(parents=True, exist_ok=True)
    entry = {"invocation": run_id, "pid": os.getpid(), "ts": "2026-10-13T10:00:00Z", "argv": ["k8s", "run"]}
    (jdir / "running" / f"{run_id}.json").write_text(json.dumps(entry))
    (jdir / "running" / f"{run_id}.logs").write_text(f"{log}\n")
    return entry


def test_follow_prints_the_log_as_it_grows_and_returns_the_exit_code(jdir, tmp_path):
    log = tmp_path / "m.log"
    log.write_text("one\n")
    run = _fake_run(jdir, log)

    def the_run():
        time.sleep(0.3)
        with log.open("a") as fh:
            fh.write("two\n")
        time.sleep(0.3)
        with (jdir / "commands.jsonl").open("a") as fh:
            fh.write(json.dumps({"invocation": "ccc333", "exit": 4, "logs": [str(log)]}) + "\n")
        with log.open("a") as fh:
            fh.write("three\n")
        (jdir / "running" / "ccc333.json").unlink()

    out = []
    thread = threading.Thread(target=the_run)
    thread.start()
    code = journal_running.follow(jdir, run, out.append, poll=0.05)
    thread.join()
    assert code == 4 and "".join(out) == "one\ntwo\nthree\n"


def test_follow_times_out_with_124_and_a_dead_run_is_none(jdir, tmp_path):
    log = tmp_path / "m.log"
    log.write_text("")
    run = _fake_run(jdir, log)
    assert journal_running.follow(jdir, run, lambda t: None, timeout=0.2, poll=0.05) == 124
    assert journal_running.follow(jdir, {**run, "pid": 999_999}, lambda t: None, poll=0.05) is None
    assert not (jdir / "running" / "ccc333.json").exists()


def test_cli_running_and_follow_an_ended_run(jdir, tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["running"])
    assert result.exit_code == 1 and "No command running" in result.output
    log = tmp_path / "m.log"
    log.write_text("hello from the remote host\n")
    _fake_run(jdir, log)
    rows = json.loads(runner.invoke(app, ["running", "--json"]).output)
    assert [r["invocation"] for r in rows] == ["ccc333"] and rows[0]["logs"] == [str(log)]
    assert "opsjournal k8s run" in runner.invoke(app, ["running"], env={"COLUMNS": "200"}).output
    assert runner.invoke(app, ["running", "-g", "pvc"]).exit_code == 1
    (jdir / "running" / "ccc333.json").unlink()
    (jdir / "commands.jsonl").write_text(json.dumps({"invocation": "ccc333", "exit": 0, "argv": ["k8s", "run"], "logs": [str(log)]}) + "\n")
    result = runner.invoke(app, ["follow", "ccc"])
    assert result.exit_code == 0 and "hello from the remote host" in result.output
    assert runner.invoke(app, ["follow", "zzz"]).exit_code == 2
    assert runner.invoke(app, ["follow", "--last"]).exit_code == 2


def test_cli_follow_needs_a_choice_when_several_run(jdir, tmp_path):
    log = tmp_path / "m.log"
    log.write_text("")
    _fake_run(jdir, log, "ddd444")
    _fake_run(jdir, log, "eee555")
    result = CliRunner().invoke(app, ["follow"])
    assert result.exit_code == 2 and "--last" in result.output
    result = CliRunner().invoke(app, ["follow", "--last", "--timeout", "0.2"])
    assert result.exit_code == 124


def test_branch_and_ticket_from_git_files(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/AI-1115-gliner\n")
    (tmp_path / "sub").mkdir()
    assert journal_running.branch_of(str(tmp_path / "sub")) == "AI-1115-gliner"
    assert journal_running.ticket_of("AI-1115-gliner") == "AI-1115"
    wt = tmp_path / "wt"
    wt.mkdir()
    (tmp_path / "gd").mkdir()
    (tmp_path / "gd" / "HEAD").write_text("ref: refs/heads/journal-follow\n")
    (wt / ".git").write_text(f"gitdir: {tmp_path / 'gd'}\n")
    assert journal_running.branch_of(str(wt)) == "journal-follow"
