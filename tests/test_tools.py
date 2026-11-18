"""tools.py: read-only enforcement, denylist, remote write, trimming, audit."""
import json
import subprocess

import pytest

import tools

RO_BLOCKED = [
    "rm -rf /tmp/x", "touch /tmp/x", "echo hi > /tmp/x", "cp a b", "rsync -a a b",
    "python3 -c \"open('/tmp/x','w').write('1')\"", "perl -e 'unlink q(x)'", "node -e 'x'",
    "bash -c 'rm x'", "sh -c id", "curl -o /tmp/x https://e.x", "curl -X POST https://e.x",
    "curl -d a=1 https://e.x", "wget https://e.x", "find /tmp -delete", "find . -exec rm {} ;",
    "ls | xargs rm", "tar xzf a.tgz", "tar -czf out.tgz dir", "git apply p.diff",
    "echo 'rm x' | bash", "python3 <<EOF", "sudo ls", "docker exec web sh",
    "docker compose up -d", "sed -i s/a/b/ f", "awk 'BEGIN{system(\"id\")}'",
]
RO_ALLOWED = [
    "ls -la /mnt/user", "df -h", "docker ps -a", "docker logs --tail 50 web", "cat /etc/hostname",
    "grep -r error /var/log/app.log", "tail -n 100 /var/log/syslog 2>&1", "free -m",
    "curl -s https://example.com/health", "git log --oneline -5", "git status", "tar -tzf box.tgz",
    "python3 --version", "journalctl -u nginx --since today", "ps aux | grep plex",
    "du -sh /mnt/user/* 2>/dev/null | sort -h", "grep at file.txt", "ssh -V",
]


@pytest.mark.parametrize("cmd", RO_BLOCKED)
def test_readonly_refuses(cmd):
    assert tools.is_destructive(cmd), cmd


@pytest.mark.parametrize("cmd", RO_ALLOWED)
def test_readonly_allows(cmd):
    assert not tools.is_destructive(cmd), cmd


def test_readonly_flag_overrides_operator_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_MODE", "operator")
    f = tmp_path / "x"
    assert tools.run_shell(f"touch {f}", readonly=True).startswith("REFUSED")
    assert not f.exists()
    tools.run_shell(f"touch {f}")                     # operator, not restricted
    assert f.exists()


def test_readonly_flag_cannot_elevate(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_MODE", "readonly")
    assert tools.run_shell(f"touch {tmp_path}/x", readonly=False).startswith("REFUSED")


def test_execute_tool_denies_writes_for_readonly(tmp_path):
    r = tools.execute_tool("write_file", {"path": str(tmp_path / "x"), "content": "1"},
                           actor="mcp:mate", readonly=True)
    assert r.startswith("DENIED") and not (tmp_path / "x").exists()


def _fake_ssh(calls):
    """Stand-in for subprocess.run that executes the remote half of an ssh argv
    locally, so the exact command string Loyd builds is really run."""
    real = subprocess.run

    def run(argv, **kw):
        calls.append(argv)
        if argv[0] == "ssh":
            return real(["bash", "-c", argv[-1]], **kw)
        return real(argv, **kw)
    return run


def test_write_file_goes_to_remote(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tools, "_REMOTE", "root@tokyo7")
    monkeypatch.setattr(tools.subprocess, "run", _fake_ssh(calls))
    target = tmp_path / "deep dir" / "it's.txt"
    body = "line1\n$HOME `id` 'q'\n"
    r = tools.write_file(str(target), body)
    assert r.startswith("Created") and "root@tokyo7" in r
    assert calls[-1][0] == "ssh" and "root@tokyo7" in calls[-1]
    assert target.read_text() == body
    assert tools.write_file(str(target), "v2").startswith("Overwrote")
    assert target.read_text() == "v2"


def test_readonly_calls_use_readonly_ssh_user(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "_REMOTE", "root@tokyo7")
    monkeypatch.setattr(tools, "_REMOTE_RO", "loyd-ro@tokyo7")
    monkeypatch.setattr(tools, "_MODE", "operator")
    monkeypatch.setattr(tools.subprocess, "run", _fake_ssh(calls))
    tools.run_shell("echo hi", readonly=True)
    assert "loyd-ro@tokyo7" in calls[-1]
    tools.read_file("/etc/hostname", readonly=True)
    assert "loyd-ro@tokyo7" in calls[-1]
    tools.run_shell("echo hi")                        # operator call keeps the main login
    assert "root@tokyo7" in calls[-1]


def test_cap_keeps_head_and_tail(monkeypatch):
    monkeypatch.setattr(tools, "MAX_OUTPUT_LINES", 30)
    monkeypatch.setattr(tools, "MAX_OUTPUT_CHARS", 10_000)
    out = tools._cap("\n".join(f"L{i}" for i in range(1000)))
    assert out.startswith("L0") and out.rstrip().endswith("L999") and "omitted" in out
    monkeypatch.setattr(tools, "MAX_OUTPUT_CHARS", 300)
    out = tools._cap("A" * 1000 + "ERROR at end")
    assert out.endswith("ERROR at end") and len(out) < 400


def test_audit_records_and_scrubs(monkeypatch, tmp_path):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("LOYD_AUDIT_LOG", str(log))
    tools.execute_tool("write_file", {"path": str(tmp_path / "f"), "content": "secret" * 100},
                       actor="mcp:me")
    tools.execute_tool("write_file", {"path": "/x", "content": "y"}, actor="mcp:mate", readonly=True)
    tools.execute_tool("run_shell", {"command": "echo ok"}, actor="cli:me")
    recs = [json.loads(l) for l in log.read_text().splitlines()]
    assert [r["actor"] for r in recs] == ["mcp:me", "mcp:mate", "cli:me"]
    assert recs[0]["args"]["content"]["chars"] == 600 and "secret" not in log.read_text()
    assert recs[1]["outcome"] == "denied" and recs[2]["outcome"] == "ok"
    assert oct(log.stat().st_mode)[-3:] == "600"


def test_audit_rotates(monkeypatch, tmp_path):
    import audit
    log = tmp_path / "a.jsonl"
    monkeypatch.setenv("LOYD_AUDIT_LOG", str(log))
    monkeypatch.setenv("LOYD_AUDIT_MAX_MB", "0.0005")      # ~500 bytes
    for _ in range(20):
        audit.record("t", "run_shell", {"command": "x" * 50}, "ok")
    assert (tmp_path / "a.jsonl.1").exists() and log.stat().st_size < 1000
