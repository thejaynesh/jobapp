"""
Looking behind employers' own careers sites for the Greenhouse board they
wrap, and the settings that control it. Hosts and board names are the live
ones measured on 2026-09-28. No network.
"""

import re
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models.profile import Profile
from app.services import ats_sniffer, tunables

WAYMO = "https://careers.withwaymo.com/jobs?gh_jid=7488508"


def _resp(text="", status=200):
    resp = MagicMock()
    resp.text, resp.status_code = text, status
    return resp


def _client(responses: dict, calls: list):
    client = MagicMock()

    def get(url, **kw):
        calls.append(url)
        return responses.get(url) or _resp(status=404)

    client.get.side_effect = get
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return ctx


class TestGuesses:
    @pytest.mark.parametrize("host,company,expected", [
        ("careers.withwaymo.com", "Waymo", "waymo"),
        ("www.ixl.com", "IXL Learning", "ixllearning"),
        ("www.tower-research.com", "Tower Research Capital", "towerresearchcapital"),
        ("www.dmcinfo.com", "DMC Engineering", "dmcengineering"),
    ])
    def test_the_board_name_is_among_them(self, host, company, expected):
        assert expected in ats_sniffer.greenhouse_guesses(host, company)

    def test_a_posting_id_is_read_from_the_link(self):
        assert ats_sniffer.greenhouse_job_id(WAYMO) == "7488508"
        assert ats_sniffer.greenhouse_job_id("https://careers.acme.com/job/1") is None


class TestSniffHost:
    def test_the_posting_page_is_read_first(self):
        """compass.com's posting page embeds its board; its careers page need not."""
        posting = "https://www.compass.com/careers/job/?gh_jid=123"
        calls = []
        responses = {posting: _resp(
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=urbancompass">')}
        with patch.object(ats_sniffer.httpx, "Client", return_value=_client(responses, calls)):
            found = ats_sniffer.sniff_host("www.compass.com", posting_url=posting,
                                           company="Compass")
        assert found == {"greenhouse": ["urbancompass"]} and calls == [posting]

    def test_a_guess_is_taken_only_when_it_holds_the_posting(self):
        calls = []
        # A board called "withwaymo" exists but is someone else's: it does not
        # have posting 7488508. "waymo" does.
        responses = {
            "https://boards-api.greenhouse.io/v1/boards/waymo/jobs/7488508": _resp("{}"),
        }
        with patch.object(ats_sniffer.httpx, "Client", return_value=_client(responses, calls)), \
             patch("app.services.ats_validation.is_valid_slug", return_value=False):
            found = ats_sniffer.sniff_host("careers.withwaymo.com", posting_url=WAYMO,
                                           company="Waymo")
        assert found == {"greenhouse": ["waymo"]}
        asked = [u for u in calls if "boards-api" in u]
        assert asked[0].endswith("/boards/withwaymo/jobs/7488508")
        assert asked[-1].endswith("/boards/waymo/jobs/7488508")

    def test_a_posting_on_another_host_is_not_read(self):
        calls = []
        with patch.object(ats_sniffer.httpx, "Client", return_value=_client({}, calls)), \
             patch("app.services.ats_validation.is_valid_slug", return_value=False):
            ats_sniffer.sniff_host("careers.acme.com",
                                   posting_url="https://evil.example.com/x?gh_jid=1")
        assert not any("evil.example.com" in u for u in calls)
        assert not any("boards-api" in u for u in calls)

    def test_hints_reach_the_sniffer(self):
        with patch.object(ats_sniffer, "sniff_host", return_value={}) as sniff:
            ats_sniffer.sniff_hosts({"careers.withwaymo.com": ""},
                                    hints={"careers.withwaymo.com": {"url": WAYMO,
                                                                     "company": "Waymo"}})
        assert sniff.call_args.kwargs == {"posting_url": WAYMO, "company": "Waymo"}


class TestListHarvest:
    def test_employer_links_are_collected_for_the_sniffer(self, monkeypatch):
        from app.services import ats_discovery
        from app.services.sources import simplify

        rows = [
            {"url": "https://careers.withwaymo.com/jobs/old-role", "company_name": "Waymo"},
            {"url": WAYMO, "company_name": "Waymo"},
            {"url": "https://careers.withwaymo.com/jobs/newer", "company_name": "Waymo"},
            {"url": "https://boards.greenhouse.io/stripe/jobs/1", "company_name": "Stripe"},
            {"url": "https://www.indeed.com/viewjob?jk=1", "company_name": "Acme"},
        ]
        monkeypatch.setattr(simplify, "rows", lambda url: rows)
        links = {}
        found, _ = ats_discovery.harvest_boards_from_lists(["https://x/listings.json"],
                                                           career_links=links)
        assert found == {"greenhouse": {"stripe"}}
        # The posting that names its Greenhouse ID is the one kept.
        assert links == {"careers.withwaymo.com": {"url": WAYMO, "company": "Waymo"}}


class TestTheSettingsPageControlsIt:
    def _sniff(self, db, overrides, links):
        profile = Profile(data={tunables.STORE_KEY: overrides})
        db.add(profile)
        db.commit()
        with patch("app.services.ats_sniffer.sniff_host", return_value={}) as sniff:
            from app.services.job_fetcher import _update_board_registry
            _update_board_registry(db, [], {}, {}, None, dict(profile.data),
                                   career_links=links)
        return sniff

    LINKS = {f"careers.acme{i}.com": {"url": f"https://careers.acme{i}.com/j?gh_jid={i}",
                                      "company": f"Acme {i}"} for i in range(5)}

    def test_list_links_are_looked_behind(self, db):
        sniff = self._sniff(db, {}, self.LINKS)
        assert sniff.call_count == 5
        assert sniff.call_args.kwargs["posting_url"].startswith("https://careers.acme")

    def test_sites_per_cycle(self, db):
        assert self._sniff(db, {"ats_sniff_max_hosts_per_cycle": 2}, self.LINKS).call_count == 2

    def test_switching_it_off(self, db):
        assert self._sniff(db, {"ats_sniff_career_sites": False}, self.LINKS).call_count == 0


def _chips(body: str) -> dict[str, bool]:
    """Each status chip on the settings page: label → shown as on."""
    return {label.strip(): colour == "bg-green-50" for colour, label in re.findall(
        r'<div class="flex items-center gap-1\.5[^"]*?(bg-green-50|bg-gray-50)[^"]*">'
        r'(?:(?!</div>).)*?<span>([^<]+)</span>', body, re.S)}


def test_the_status_panel_shows_what_the_settings_page_set(client, db):
    """A source switched off on this page used to read as on, from `.env`."""
    db.add(Profile(data={tunables.STORE_KEY: {"tiktok_enabled": False}}))
    db.commit()
    chips = _chips(client.get("/settings").text)
    assert chips["TikTok Careers"] is False
    assert chips["Apple Jobs"] is True
