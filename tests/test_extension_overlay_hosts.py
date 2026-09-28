"""
Where the extension's panel draws (`extension/overlay_hosts.js`), run in Node.

The core list is pinned: it is what every earlier version asked permission
for, and the registration checks that grant, so changing it would switch the
panel off on every existing install at update. Skips where Node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="no Node to run the extension's modules")

# What versions up to 0.3.0 asked for. Not to be edited: see the module docstring.
GRANTED_BEFORE = [
    "https://www.linkedin.com/jobs/*",
    "https://boards.greenhouse.io/*",
    "https://job-boards.greenhouse.io/*",
    "https://jobs.lever.co/*",
    "https://jobs.ashbyhq.com/*",
    "https://*.myworkdayjobs.com/*",
    "https://apply.workable.com/*",
    "https://jobs.smartrecruiters.com/*",
    "https://*.recruitee.com/*",
]

_PATTERN = re.compile(r"^https://(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+/[A-Za-z0-9_/-]*\*$")


def node(script: str):
    out = subprocess.run(
        [NODE, "--no-warnings", "--input-type=module", "-e",
         "import * as m from './extension/overlay_hosts.js';\n" + script],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_the_core_list_is_what_existing_installs_granted():
    assert node("console.log(JSON.stringify(m.OVERLAY_CORE))") == GRANTED_BEFORE


def test_every_pattern_is_a_valid_match_pattern_and_listed_once():
    lists = node("console.log(JSON.stringify([m.OVERLAY_CORE, m.OVERLAY_MORE]))")
    core, more = lists
    assert all(_PATTERN.match(p) for p in core + more), [p for p in core + more
                                                         if not _PATTERN.match(p)]
    assert len(set(core + more)) == len(core) + len(more)


@pytest.mark.parametrize("state,expected", [
    ({"overlay": False, "more": True, "hasCore": True, "hasMore": True}, None),
    ({"overlay": True, "more": True, "hasCore": False, "hasMore": True}, None),
    ({"overlay": True, "more": False, "hasCore": True, "hasMore": True}, "core"),
    ({"overlay": True, "more": True, "hasCore": True, "hasMore": False}, "core"),
    ({"overlay": True, "more": True, "hasCore": True, "hasMore": True}, "all"),
])
def test_where_the_panel_is_registered(state, expected):
    got = node(f"console.log(JSON.stringify([m.overlayMatches({json.dumps(state)}), "
               "m.OVERLAY_CORE, m.OVERLAY_MORE]))")
    matches, core, more = got
    want = {None: None, "core": core, "all": core + more}[expected]
    assert matches == want


def test_the_panels_files_load_autofill_first():
    assert node("console.log(JSON.stringify(m.OVERLAY_FILES))") == ["autofill.js", "overlay.js"]


def test_the_systems_the_fill_reads_have_their_hosts_listed():
    core, more = node("console.log(JSON.stringify([m.OVERLAY_CORE, m.OVERLAY_MORE]))")
    listed = " ".join(core + more)
    for host in ("icims.com", "taleo.net", "oraclecloud.com", "successfactors.com",
                 "recruiting.paylocity.com", "ultipro.com", "dayforcehcm.com",
                 "bamboohr.com", "applytojob.com", "eightfold.ai"):
        assert host in listed, host


# The service worker itself, loaded in Node with `chrome` stubbed
# (tests/js/background_harness.mjs), then asked where it left the panel.
HARNESS = ROOT / "tests" / "js" / "background_harness.mjs"
BACKGROUND = ROOT / "extension" / "background.js"


def registration_after_startup(state: dict):
    out = subprocess.run(
        [NODE, "--no-warnings", str(HARNESS), json.dumps(state), str(BACKGROUND)],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _more():
    return node("console.log(JSON.stringify(m.OVERLAY_MORE))")


class TestTheServiceWorker:
    def test_an_existing_install_keeps_its_panel_after_the_update(self):
        """Registered by 0.2.x: core sites, overlay.js alone, core granted."""
        registration = registration_after_startup({
            "stored": {"overlay": True},
            "granted": GRANTED_BEFORE,
            "registered": [{"id": "jobapp-overlay", "matches": GRANTED_BEFORE,
                            "js": ["overlay.js"], "runAt": "document_idle"}],
        })
        assert registration["matches"] == GRANTED_BEFORE
        assert registration["js"] == ["autofill.js", "overlay.js"]

    def test_the_other_systems_join_once_ticked_and_granted(self):
        more = _more()
        registration = registration_after_startup({
            "stored": {"overlay": True, "overlayMore": True},
            "granted": GRANTED_BEFORE + more,
        })
        assert registration["matches"] == GRANTED_BEFORE + more

    def test_ticked_but_not_granted_is_the_core_list(self):
        registration = registration_after_startup({
            "stored": {"overlay": True, "overlayMore": True},
            "granted": GRANTED_BEFORE,
        })
        assert registration["matches"] == GRANTED_BEFORE

    def test_switched_off_is_unregistered(self):
        assert registration_after_startup({
            "stored": {"overlay": False},
            "granted": GRANTED_BEFORE,
            "registered": [{"id": "jobapp-overlay", "matches": GRANTED_BEFORE,
                            "js": ["autofill.js", "overlay.js"]}],
        }) is None
