"""
Every setting is either on the settings page or deliberately not, and a
setting on the page is the one the code reads.

CLAUDE.md's standing rule is that anything changing how the application
behaves is editable from the settings page. It was broken 150 times over
without anybody deciding to break it: each knob was added to `config.py`,
read as `settings.X`, and that was the end of it. These tests make the rule
mechanical — a new `Settings` field has to be declared as a tunable or named
in `tunables.ENVIRONMENT` with a reason, and code may not read a tunable
straight off `settings`, where the page's value can never reach it.
"""

import ast
import re
from pathlib import Path

from app.config import Settings
from app.services import tunables

ROOT = Path(__file__).resolve().parent.parent
DECLARED = {t.env for t in tunables.TUNABLES if t.env}


def test_every_setting_is_on_the_page_or_named_as_environment():
    unclassified = sorted(
        name for name in Settings.model_fields
        if name not in DECLARED and name not in tunables.ENVIRONMENT
    )
    assert not unclassified, (
        "Declare these in tunables.TUNABLES, or name them in "
        f"tunables.ENVIRONMENT with the reason they stay in .env: {unclassified}"
    )


def test_nothing_is_both():
    assert not DECLARED & set(tunables.ENVIRONMENT)


def test_the_environment_list_names_real_settings():
    assert set(tunables.ENVIRONMENT) <= set(Settings.model_fields)


def test_every_tunable_starts_in_env_example():
    """`.env.example` is the canonical list for a first deploy (CLAUDE.md)."""
    text = (ROOT / ".env.example").read_text()
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
    missing = sorted(DECLARED - listed)
    assert not missing, f"Add to .env.example with a comment: {missing}"


def _direct_reads(path: Path) -> list[str]:
    """
    `settings.X` and `getattr(settings, "X")` for a tunable X — as code, not
    in a comment or docstring — including through any name the module gives
    `app.config.settings` (`from app.config import settings as cfg`).
    """
    tree = ast.parse(path.read_text())
    names = {"settings"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.config":
            names |= {a.asname for a in node.names if a.name == "settings" and a.asname}
    found = []
    for node in ast.walk(tree):
        env = None
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in names):
            env = node.attr
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "getattr" and len(node.args) >= 2
              and isinstance(node.args[0], ast.Name) and node.args[0].id in names
              and isinstance(node.args[1], ast.Constant)):
            env = node.args[1].value
        if env in DECLARED:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} {env}")
    return found


def test_no_code_reads_a_tunable_straight_off_settings():
    """
    A consumer reading `settings.X` sees the environment only, and silently
    ignores whatever the page says — the control renders, saves, and changes
    nothing. Read through `tunables.live()`, `tunables.value()`, or the `cfg`
    overlay a fetch cycle hands down.
    """
    allowed = {ROOT / "app" / "config.py", ROOT / "app" / "services" / "tunables.py"}
    violations = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path in allowed:
            continue
        violations += _direct_reads(path)
    assert not violations, "\n".join(violations)


def test_every_scheduled_interval_is_a_setting():
    from app.tasks.schedule import SCHEDULE

    for entry in SCHEDULE:
        assert entry.tunable in tunables.BY_KEY, entry.name
