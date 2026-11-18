"""deploy/deploy.sh: dry run changes nothing; a near-real run (real tests + real
smoke server, fake systemd) switches, and its health check rolls back."""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "deploy.sh")
pytestmark = pytest.mark.skipif(os.getenv("LOYD_DEPLOY_INNER") == "1" or not shutil.which("git"),
                                reason="inner run / no git")


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def site(tmp_path):
    # A private "origin" with a new commit on branch deploytest; the live checkout
    # sits one commit behind it. No dependence on this repo's history depth (CI clones shallow).
    origin = tmp_path / "origin"
    subprocess.run(["git", "clone", "-q", "--no-local", ROOT, str(origin)], check=True)
    live = tmp_path / "loyd"
    subprocess.run(["git", "clone", "-q", str(origin), str(live)], check=True)
    _git("checkout", "-q", "--detach", cwd=live)
    prev = _git("rev-parse", "--short", "HEAD", cwd=live)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
         "-m", "new release", cwd=origin)
    _git("branch", "-q", "-f", "deploytest", cwd=origin)
    head, branch = _git("rev-parse", "--short", "HEAD", cwd=origin), "deploytest"
    env_file = tmp_path / "loyd.env"
    env_file.write_text("LOYD_MCP_TOKENS=me:o:" + "t" * 32 + "\nLOYD_MCP_PATH=/tokyo7\n"
                        "LOYD_MCP_PUBLIC_URL=https://loyd.example\nLOYD_MCP_PORT=18999\n")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("systemd-run", "systemctl", "journalctl"):
        (fakebin / name).write_text(f"#!/bin/bash\necho {name} \"$@\" >> {calls}\nexit 0\n")
        (fakebin / name).chmod(0o755)
    env = dict(os.environ, LOYD_DIR=str(live), ENV_FILE=str(env_file), NEXT=str(tmp_path / "next"),
               PY=sys.executable, LOG=str(tmp_path / "deploy.log"), CHECK_DIR=str(tmp_path),
               SPARE_PORT="18998", PYTEST_ARGS="tests/test_kb.py", LOYD_DEPLOY_INNER="1",
               PATH=f"{fakebin}:{os.environ['PATH']}")
    return dict(live=live, branch=branch, head=head, prev=prev, env=env, env_file=env_file,
                calls=calls, tmp=tmp_path)


def test_dry_run_changes_nothing(site):
    out = subprocess.run([SCRIPT, site["branch"], "--dry-run"], env=site["env"],
                         capture_output=True, text=True, check=True).stdout
    for step in ("1/4", "2/4", "3/4", "4/4"):
        assert step in out
    assert "DRY: systemd-run" in out and "rolling back to " + site["prev"] in out
    assert "\\$" not in out                                              # check script fully rendered
    assert _git("rev-parse", "--short", "HEAD", cwd=site["live"]) == site["prev"]
    assert not site["calls"].exists()


def test_real_run_switches_then_health_check_rolls_back(site):
    p = subprocess.run([SCRIPT, site["branch"]], env=site["env"], capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "smoke: healthz, OAuth metadata, 401 on /tokyo7, tools/list all OK" in p.stdout
    assert _git("rev-parse", "--short", "HEAD", cwd=site["live"]) == site["head"]   # switched
    calls = site["calls"].read_text()
    assert "--on-active=3 /bin/systemctl restart loyd-mcp-tokyo7" in calls and "--on-active=45" in calls
    # Now play the scheduled health check: nothing listens on 18999 -> unhealthy -> rollback.
    site["env_file"].write_text(site["env_file"].read_text() + "BROKEN=1\n")
    check = next(site["tmp"].glob("loyd-deploy-check-*.sh"))
    subprocess.run([str(check)], env=site["env"], check=True)
    assert _git("rev-parse", "--short", "HEAD", cwd=site["live"]) == site["prev"]
    assert "BROKEN" not in site["env_file"].read_text()                 # env backup restored
    assert "UNHEALTHY" in (site["tmp"] / "deploy.log").read_text()
