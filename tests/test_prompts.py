"""prompts.py: the ticket in a branch name, and a long prompt kept short."""
from opsjournal import prompts


def test_ticket_of():
    assert prompts.ticket_of("AI-1115") == "AI-1115"
    assert prompts.ticket_of("feature/CW-3220-mimir") == "CW-3220"
    assert prompts.ticket_of("own-gliner-docs") is None
    assert prompts.ticket_of("main") is None and prompts.ticket_of(None) is None


def test_long_prompt_truncated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_SESSION", "x")
    line = prompts.entry({"prompt": "a" * (prompts.MAX_CHARS + 5), "cwd": str(tmp_path)})
    assert len(line["prompt"]) == prompts.MAX_CHARS and line["truncated"] == prompts.MAX_CHARS + 5
