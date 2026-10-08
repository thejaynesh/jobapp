"""An unfamiliar source reaches the same opt-in reader registration as built-in sites."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for extension modules")
ORIGIN = "https://careers.new-employer.example"
SITE_ID = "custom-" + ORIGIN.encode().hex()


def registered(*, enabled, permitted):
    state = {"stored": {"customHarvestOrigins": [ORIGIN], f"harvest-{SITE_ID}": enabled},
             "granted": [ORIGIN + "/*"] if permitted else []}
    result = subprocess.run([NODE, "--no-warnings", str(ROOT / "tests/js/background_harness.mjs"),
        json.dumps(state), str(ROOT / "extension/background.js"), "all"],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
    return [row for row in json.loads(result.stdout.strip().splitlines()[-1]) if SITE_ID in row["id"]]


def test_custom_source_permission_and_crawl_gates():
    subprocess.run([NODE, "--no-warnings", str(ROOT / "tests/js/source_learning_sites.mjs")],
                   cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)


@pytest.mark.parametrize("enabled,permitted", [(False, False), (False, True), (True, False)])
def test_custom_origin_needs_both_toggle_and_permission(enabled, permitted):
    assert registered(enabled=enabled, permitted=permitted) == []


def test_allowed_custom_origin_registers_interceptor_and_relay():
    scripts = registered(enabled=True, permitted=True)
    assert {tuple(row["js"]) for row in scripts} == {("interceptor.js",), ("relay.js",)}
    assert all(row["matches"] == [ORIGIN + "/*"] for row in scripts)
    assert next(row for row in scripts if row["js"] == ["interceptor.js"])["world"] == "MAIN"
