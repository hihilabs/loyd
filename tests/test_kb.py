"""Knowledge base search: ranking, scoping, path safety."""
import kb
import tools


def test_finds_the_right_section():
    out = kb.search_kb("gpu1 vision worker port 11435")
    first = out.split("\n", 1)[0]
    assert "kb/tokyo7.md" in first


def test_outage_runbook_for_dns():
    hits = kb.search("DNS failing 1.1.1.1 answers")
    assert hits and hits[0][1] == "kb/runbooks/outage.md"


def test_repo_docs_are_searchable():
    assert any(h[1] == "MCP.md" for h in kb.search("OAuth refresh token read-only checkbox", 10))


def test_no_match_and_empty():
    assert kb.search_kb("zzqqxxnonexistent").startswith("No notes match")
    assert kb.search("") == []


def test_read_kb_only_known_notes():
    assert "Runbook: something is out" in kb.read_kb("kb/runbooks/outage.md")
    for bad in ("../tools.py", "/etc/passwd", "kb/../../etc/passwd"):
        assert kb.read_kb(bad).startswith("ERROR")


def test_extra_dirs(monkeypatch, tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "router.md").write_text("# Router\nThe router is a GL.iNet Flint with AdGuard.\n")
    monkeypatch.setenv("LOYD_KB_DIRS", str(tmp_path / "notes"))
    assert kb.search("adguard flint")[0][1] == "notes/router.md"


def test_registered_as_read_only_tools():
    names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
    assert "search_kb" in names and "search_kb" not in tools.WRITE_TOOLS
    assert "Runbook" in tools.execute_tool("read_kb", {"name": "kb/runbooks/outage.md"}, readonly=True)


def test_notes_carry_a_source_tag():
    import os
    for name, path in kb._files().items():
        if name.startswith("kb/"):
            assert open(path).readline().startswith("<!-- source:"), name
