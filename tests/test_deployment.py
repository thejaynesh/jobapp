"""Execute the deployment controller against simulated Docker failure states."""
import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="VPS deployment uses Bash")
IMAGE = "ghcr.io/example/jobapp@sha256:" + "a" * 64

DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["DEPLOY_DIR"])
env = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)
with (root / "commands.jsonl").open("a") as output:
    output.write(json.dumps({"args": args, "image": os.environ.get("APP_IMAGE", env.get("APP_IMAGE"))}) + "\n")
mode = os.environ["DEPLOY_TEST_MODE"]
if args[0] == "--config":
    if "login" in args:
        sys.stdin.read()
elif args[0] == "inspect":
    if args[2] == '{{.State.Running}}':
        print("false" if mode == "stopped_redis" else "true")
    else:
        print("sha256:" + "b" * 64 if args[-1] == "old-web" else "existing-redis-volume")
elif args[:2] == ["exec", "old-redis"]:
    if "ping" in args:
        print("PONG")
    elif "CONFIG" in args:
        print("OK")
    else:
        print("aof_enabled:1\naof_rewrite_in_progress:0\naof_rewrite_scheduled:0\naof_last_bgrewrite_status:" + ("err" if mode == "redis_failure" else "ok"))
elif args[0] == "compose":
    command = args[3:]
    if command[:1] == ["ps"]:
        print("old-web" if command[-1] == "web" else "old-redis")
    elif command[:1] == ["run"] and mode == "migration_failure":
        sys.exit(1)
    elif command[:3] == ["exec", "-T", "web"] and mode == "readiness_failure":
        sys.exit(1)
'''


@pytest.mark.parametrize("mode", ["success", "stopped_redis", "migration_failure", "readiness_failure", "redis_failure"])
def test_deployment_preserves_data_and_restores_failed_release(tmp_path, mode):
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "docker").write_text(DOCKER)
    (binary / "docker").chmod(0o755)
    (binary / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (binary / "sleep").chmod(0o755)
    (tmp_path / ".env").write_text("API_KEY=keep-original\nAPP_IMAGE=jobapp-app\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy-vps.sh"
    env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
           "DEPLOY_DIR": str(tmp_path), "DEPLOY_TEST_MODE": mode,
           "REGISTRY_TOKEN": "never-print-this-token", "REGISTRY_USERNAME": "test"}
    run = subprocess.run(["bash", str(script), IMAGE], env=env, capture_output=True, text=True, timeout=30)
    entries = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    saved = (tmp_path / ".env").read_text()
    assert "API_KEY=keep-original" in saved
    assert "REDIS_DATA_VOLUME=existing-redis-volume" in saved
    assert "never-print-this-token" not in run.stdout + run.stderr
    assert not any("build" in entry["args"] for entry in entries)
    login = next(entry for entry in entries if "login" in entry["args"])
    assert not Path(login["args"][1]).exists(), "temporary registry credentials must be removed"
    if mode in ("success", "stopped_redis"):
        assert run.returncode == 0, run.stderr
        assert "APP_IMAGE=" + IMAGE in saved
        migration = next(entry for entry in entries if "alembic" in entry["args"])
        assert migration["image"] == IMAGE
        assert any("reload" in entry["args"] for entry in entries)
        if mode == "stopped_redis":
            start = next(i for i, entry in enumerate(entries) if entry["args"] == ["start", "old-redis"])
            enable = next(i for i, entry in enumerate(entries) if "CONFIG" in entry["args"])
            assert start < enable
    elif mode == "redis_failure":
        assert run.returncode != 0
        assert "APP_IMAGE=jobapp-app" in saved
        assert not any("stop" in entry["args"] for entry in entries)
    else:
        assert run.returncode != 0
        previous = "sha256:" + "b" * 64
        assert "APP_IMAGE=" + previous in saved
        assert entries[-1]["image"] == previous
        assert entries[-1]["args"][-4:] == ["web", "worker", "worker-interactive", "beat"]
