"""run_in_tmux / job_output / tmux_read: real tmux, isolated per test."""
import shutil
import subprocess
import time

import pytest

import tools

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tools, "JOB_LOG_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(tools, "_REMOTE", "")
    monkeypatch.setattr(tools, "_MODE", "operator")
    yield
    subprocess.run(["tmux", "-L", tools.JOB_SERVER, "kill-server"], capture_output=True)
    subprocess.run(["tmux", "kill-server"], capture_output=True)


def _wait_finished(name, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        out = tools.job_output(name)
        if "status: finished" in out:
            return out
        time.sleep(0.2)
    raise AssertionError(out)


def test_job_runs_in_background_and_reports_exit():
    t0 = time.time()
    out = tools.run_in_tmux("build", "echo step1; sleep 1; echo step2")
    assert out.startswith("started job build") and time.time() - t0 < 1   # returned before the job ended
    assert "status: running" in tools.job_output("build")
    done = _wait_finished("build")
    assert "loyd-job exit 0" in done and "step1" in done and "step2" in done


def test_failing_job_reports_its_exit_code():
    tools.run_in_tmux("bad", "echo oops >&2; exit 3")
    done = _wait_finished("bad")
    assert "loyd-job exit 3" in done and "oops" in done


def test_duplicate_job_name_refused():
    tools.run_in_tmux("dup", "sleep 5")
    assert tools.run_in_tmux("dup", "echo again").startswith("ERROR: a job named dup")


def test_job_with_quotes_and_pipes():
    tools.run_in_tmux("quote", "printf '%s\\n' \"it's ok\" | tr a-z A-Z")
    assert "IT'S OK" in _wait_finished("quote")


@pytest.mark.parametrize("name", ["Bad", "a b", "../x", "x;rm", "", "-x"])
def test_bad_job_names(name):
    assert tools.run_in_tmux(name, "echo hi").startswith("ERROR")
    assert tools.job_output(name).startswith("ERROR")


def test_same_rules_as_run_shell(monkeypatch):
    assert tools.run_in_tmux("wipe", "mkfs.ext4 /dev/sda").startswith("REFUSED")      # catastrophic
    assert tools.run_in_tmux("rm", "rm -rf /tmp/x", readonly=True).startswith("REFUSED")
    monkeypatch.setattr(tools, "_MODE", "readonly")
    assert tools.run_in_tmux("rm", "rm -rf /tmp/x").startswith("REFUSED")
    assert tools.run_in_tmux("du", "du -sh /tmp").startswith("started")             # read-only job ok


def test_jobs_never_touch_the_users_tmux_server():
    tools.run_in_tmux("iso", "sleep 2")
    user = subprocess.run(["tmux", "ls"], capture_output=True, text=True)
    assert "jobs" not in user.stdout                                               # default server untouched


def test_tmux_read_reads_a_user_pane():
    subprocess.run(["tmux", "new-session", "-d", "-s", "dev", "-n", "build",
                    "bash --norc -c 'echo BUILD FAILED: missing module foo; sleep 30'"], check=True)
    time.sleep(0.5)
    assert "dev:0.0" in tools.tmux_read("") or "dev:" in tools.tmux_read("")
    assert "BUILD FAILED: missing module foo" in tools.tmux_read("dev:build")
    assert tools.tmux_read("dev;rm -rf /").startswith("ERROR")


def test_unknown_job():
    assert "no job named ghost" in tools.job_output("ghost")
