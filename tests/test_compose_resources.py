"""
The CPU limits in docker-compose.prod.yml.

The VPS has two cores. With no limits, fetching, matching and the Playwright
browser could take all of both, and the portal answered 502 while they did.
These hold the two things that stop that: the workers are capped, and the web
app and its database outrank background work when the cores are contended.
"""

import re
from pathlib import Path

import yaml

COMPOSE = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "docker-compose.prod.yml").read_text()
)
SERVICES = COMPOSE["services"]


def _cap(service: str) -> float:
    raw = str(SERVICES[service]["cpus"])
    match = re.search(r":-([0-9.]+)\}", raw)
    return float(match.group(1) if match else raw)


def test_the_workers_together_leave_the_web_app_room():
    assert _cap("worker") + _cap("worker-interactive") <= 1.5


def test_the_web_app_and_database_outrank_background_work():
    web = SERVICES["web"]["cpu_shares"]
    db = SERVICES["postgres"]["cpu_shares"]
    for worker in ("worker", "worker-interactive"):
        assert SERVICES[worker]["cpu_shares"] < db < web


def test_the_ceilings_are_set_from_the_environment():
    assert "WORKER_CPUS" in str(SERVICES["worker"]["cpus"])
    assert "INTERACTIVE_WORKER_CPUS" in str(SERVICES["worker-interactive"]["cpus"])
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert "WORKER_CPUS=" in example and "INTERACTIVE_WORKER_CPUS=" in example
