"""scripts/gpu_mode.sh: dry run changes nothing; offline is refused while vision is
busy outside the night window (and never pauses vision); --apply talks to ollama and
records the mode. docker / nvidia-smi / curl are fakes on PATH."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "gpu_mode.sh")
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="no bash")


@pytest.fixture()
def box(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "calls.log"
    state = tmp_path / "state" / "gpu-mode"
    # docker inspect -> $FAKE_RUNNING; nvidia-smi -> $FAKE_UTIL; curl logs its args and
    # answers /api/ps with $FAKE_PS. Any other docker verb (stop/pause/...) is recorded.
    (fakebin / "docker").write_text(
        f'#!/bin/bash\necho docker "$@" >> {calls}\n'
        '[ "$1" = inspect ] && { echo "${FAKE_RUNNING:-true}"; exit 0; }\nexit 0\n')
    (fakebin / "nvidia-smi").write_text(
        f'#!/bin/bash\necho nvidia-smi "$@" >> {calls}\necho "${{FAKE_UTIL:-0}}"\n')
    (fakebin / "curl").write_text(
        f'#!/bin/bash\necho curl "$@" >> {calls}\n'
        '[ -n "$FAKE_CURL_FAIL" ] && exit 7\n'
        'for a in "$@"; do case "$a" in */api/ps) echo "${FAKE_PS:-{\\"models\\":[]}}";; esac; done\nexit 0\n')
    for f in fakebin.iterdir():
        f.chmod(0o755)
    cfg = tmp_path / "gpu-mode.env"
    cfg.write_text("VISION_CONTAINER=vision-gpu1\nVISION_GPU=1\nOLLAMA_URL=http://ollama:11435/\n"
                   f"OFFLINE_MODEL=big-model:32b\nSTATE_FILE={state}\nNIGHT_HOURS=23-6\n")
    env = dict(os.environ, GPU_MODE_CONFIG=str(cfg), GPU_MODE_HOUR="14",
               PATH=f"{fakebin}:{os.environ['PATH']}")
    return dict(env=env, calls=calls, state=state, cfg=cfg)


def run(box, *args, **env):
    return subprocess.run([SCRIPT, *args], env=dict(box["env"], **env), capture_output=True, text=True)


def calls(box):
    return box["calls"].read_text() if box["calls"].exists() else ""


def test_usage_and_missing_config(box, tmp_path):
    assert run(box).returncode == 2
    assert run(box, "sideways").returncode == 2
    assert run(box, "online", "--yes").returncode == 2
    r = run(box, "status", GPU_MODE_CONFIG=str(tmp_path / "nope.env"))
    assert r.returncode == 2 and "gpu-mode.env.example" in r.stderr
    box["cfg"].write_text("VISION_GPU=1\n")
    r = run(box, "status")
    assert r.returncode == 2 and "VISION_CONTAINER is required" in r.stderr


def test_config_is_read_not_executed(box, tmp_path):
    marker = tmp_path / "pwned"
    box["cfg"].write_text(box["cfg"].read_text() + f"touch {marker}\nOFFLINE_MODEL=$(touch {marker})\n")
    r = run(box, "offline")
    assert r.returncode == 2 and "bad value" in r.stderr
    assert not marker.exists()


def test_online_dry_run_changes_nothing(box):
    r = run(box, "online")
    assert r.returncode == 0, r.stderr
    assert "DRY: unload big-model:32b from http://ollama:11435" in r.stdout
    assert "nothing changed" in r.stdout
    assert "curl" not in calls(box) and not box["state"].exists()


def test_offline_refused_while_vision_busy_in_daytime(box):
    r = run(box, "offline", "--apply", FAKE_UTIL="87")
    assert r.returncode == 3
    assert "busy (GPU1 at 87%)" in r.stderr and "never paused" in r.stderr
    assert "curl" not in calls(box) and not box["state"].exists()
    assert "docker stop" not in calls(box) and "docker pause" not in calls(box)


def test_offline_unknown_utilisation_counts_as_busy(box):
    fake = os.path.join(box["env"]["PATH"].split(":")[0], "nvidia-smi")
    with open(fake, "w") as f:                                         # no reading, as on a box without drivers
        f.write("#!/bin/bash\nexit 9\n")
    r = run(box, "offline", "--apply")
    assert r.returncode == 3 and "utilisation unknown" in r.stderr


def test_offline_allowed_at_night_without_touching_vision(box):
    for hour in ("23", "03"):
        r = run(box, "offline", FAKE_UTIL="87", GPU_MODE_HOUR=hour)
        assert r.returncode == 0, r.stderr
        assert "night window 23-6" in r.stdout and "it keeps running" in r.stdout
    assert run(box, "offline", FAKE_UTIL="87", GPU_MODE_HOUR="06").returncode == 3
    c = calls(box)
    assert "docker stop" not in c and "docker pause" not in c and "docker kill" not in c


def test_offline_apply_loads_model_and_records_mode(box):
    r = run(box, "offline", "--apply", FAKE_UTIL="4")
    assert r.returncode == 0, r.stderr
    assert "vision idle (GPU1 at 4%)" in r.stdout
    c = calls(box)
    assert "http://ollama:11435/api/generate" in c
    assert '"model":"big-model:32b","keep_alive":"30m"' in c
    st = box["state"].read_text()
    assert "mode=offline" in st and "model=big-model:32b" in st and "keep_alive=30m" in st


def test_stopped_vision_container_counts_as_idle(box):
    r = run(box, "offline", FAKE_UTIL="99", FAKE_RUNNING="false")
    assert r.returncode == 0 and "container vision-gpu1: false" in r.stdout


def test_online_apply_unloads_and_records_mode(box):
    r = run(box, "online", "--apply")
    assert r.returncode == 0, r.stderr
    assert '"model":"big-model:32b","keep_alive":0' in calls(box)
    st = box["state"].read_text()
    assert "mode=online" in st and "keep_alive=2m" in st


def test_failed_ollama_call_exits_4_and_keeps_old_state(box):
    run(box, "online", "--apply")
    r = run(box, "offline", "--apply", FAKE_CURL_FAIL="1")
    assert r.returncode == 4 and "failed: load big-model:32b" in r.stderr
    assert "mode=online" in box["state"].read_text()


def test_status(box):
    run(box, "online", "--apply")
    r = run(box, "status", FAKE_UTIL="50", FAKE_PS='{"models":[{"name":"vision:7b"},{"name":"qwen:7b"}]}')
    assert r.returncode == 0, r.stderr
    assert "recorded mode: online" in r.stdout
    assert "busy (GPU1 at 50%)" in r.stdout and "night window:  no (23-6)" in r.stdout
    assert "  vision:7b\n  qwen:7b" in r.stdout
    r = run(box, "status")
    assert "(nothing loaded)" in r.stdout
    r = run(box, "status", FAKE_CURL_FAIL="1")
    assert "(ollama not reachable)" in r.stdout
