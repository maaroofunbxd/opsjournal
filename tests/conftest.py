import dataclasses

import pytest
from agentboard import tools as agent_tools

from opsjournal import config, journal


@pytest.fixture(autouse=True)
def _own_settings(tmp_path, monkeypatch):
    """Every test starts from the defaults, with the backup state in tmp_path and no agent, runner or parent run
    in the environment."""
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.Settings(), state_file=lambda: tmp_path / "sync.json"))
    monkeypatch.setenv("AGENTS_CONFIG", str(tmp_path / "agentboard-config.toml"))
    for var, _ in agent_tools.detect_env():
        monkeypatch.delenv(var, raising=False)
    for var in ("AGENTS_ACTOR", "AGENTS_SESSION", "GITHUB_ACTIONS", "GITHUB_ACTOR", "GITHUB_RUN_ID", "OPSJOURNAL_RUN_LOGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPSJOURNAL_PARENT", "")
    monkeypatch.setenv("OPSJOURNAL_JOURNAL_SYNC", "off")
    monkeypatch.setattr(journal, "nudge", lambda argv: None)
    journal._run.clear()


@pytest.fixture
def jdir(tmp_path, monkeypatch):
    """A journal in tmp_path/j."""
    monkeypatch.setenv("OPSJOURNAL_DIR", str(tmp_path / "j"))
    return tmp_path / "j"
