"""
Built In: what its cards state, how far its search is read, and the settings
that control both. No network — every page is served from the markup below,
trimmed from a live search on 2026-09-28.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.models.profile import Profile
from app.services import tunables
from app.services.sources import builtin
from app.services.sources.base import SourceUnavailable
from app.services.sources.listing_fallbacks import (
    _builtin_card_details, extract_listing_jobs,
)


def card(job_id, title="Software Engineer I", company="Chewy", age="Yesterday",
         mode="Hybrid", place="Bellevue, WA, USA", pay="112K-160K Annually",
         level="Junior"):
    """One card as Built In renders it: pay and level printed twice."""
    def row(icon, text):
        return (f'<div class="d-flex align-items-start gap-sm"><div class="d-flex '
                f'h-lg min-w-md"><i class="fa-regular {icon} fs-xs"></i></div> '
                f'<span class="font-barlow text-gray-04">{text}</span></div>')

    return (
        f'<div data-id="job-card" x-init="initObserver({job_id}, true)"><div id="main" class="row">'
        f'<a href="/company/x" data-id="company-title"><span>{company}</span></a>'
        f'<h2><a href="/job/software-engineer-i/{job_id}" data-id="job-card-title">{title}</a></h2>'
        f'<div class="d-flex align-items-start gap-sm position-relative">'
        f'<span x-show="!showSavedTag"><i class="fa-regular fa-clock fs-xs"></i>{age}</span>'
        f'<span x-show="showSavedTag === true"><i class="fa-solid fa-heart"></i>Saved </span></div>'
        f'<div class="d-flex gap-md">{row("fa-house-building", mode)}</div>'
        f'<div class="d-flex align-items-start gap-sm"><div class="d-flex"><i class="fa-regular '
        f'fa-location-dot"></i></div> <div><span class="font-barlow">{place}</span></div></div>'
        f'<div class="d-none d-xl-block fill-even">{row("fa-sack-dollar", pay)}'
        f'{row("fa-trophy", level)}</div></div>'
        f'<div id="drop-data-{job_id}" class="collapse"><div class="d-flex d-xl-none">'
        f'{row("fa-sack-dollar", pay)}{row("fa-trophy", level)}</div>'
        f'<div class="fs-sm">A summary of the role.</div></div></div>'
    )


def page(*cards):
    return f"<html><body>{''.join(cards)}</body></html>"


def serve(monkeypatch, pages: dict, calls: list | None = None):
    """Serve `pages[(path, page_number)]`, and an empty page past the end."""
    def get(url, **kw):
        if calls is not None:
            calls.append(url)
        parts = urlsplit(url)
        number = int((parse_qs(parts.query).get("page") or ["1"])[0])
        text = pages.get((parts.path, number), "<html></html>")
        return httpx.Response(200, text=text, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)


class TestWhatACardStates:
    def test_age_pay_and_level_come_off_the_card(self):
        (job,) = extract_listing_jobs(page(card(11387823)), "https://builtin.com/jobs",
                                      "builtin", "")
        assert job["title"] == "Software Engineer I" and job["company"] == "Chewy"
        assert job["location"] == "Bellevue, WA, USA; Hybrid"
        assert (job["salary_min"], job["salary_max"]) == (112000, 160000)
        assert (job["salary_currency"], job["salary_period"]) == ("USD", "year")
        # "Junior" once, although the card prints it twice.
        assert job["experience_level"] == "entry"
        posted = datetime.fromisoformat(job["posted_at"])
        assert timedelta(hours=23) < datetime.now(timezone.utc) - posted < timedelta(hours=25)

    @pytest.mark.parametrize("age,days", [
        ("Yesterday", 1), ("2 Days Ago", 2), ("Reposted 3 Days Ago", 3),
        ("Reposted Yesterday", 1), ("5 Hours Ago", 5 / 24), ("30+ Days Ago", 30),
        ("2 Weeks Ago", 14),
    ])
    def test_every_age_on_the_page_becomes_a_date(self, age, days):
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        found = _builtin_card_details("Chicago, IL, USA", age, "", "", now=now)
        assert found["posted_at"] == (now - timedelta(days=days)).isoformat()

    def test_an_age_it_cannot_read_is_left_unknown(self):
        assert "posted_at" not in _builtin_card_details("", "Featured", "", "")

    @pytest.mark.parametrize("level,expected", [
        ("Internship", "entry"), ("Entry level", "entry"), ("Junior", "entry"),
        ("Mid level", "mid"), ("Senior level", "senior"), ("Expert/Leader", "senior"),
    ])
    def test_the_boards_own_seniority_is_used(self, level, expected):
        assert _builtin_card_details("", "", "", level)["experience_level"] == expected

    def test_a_senior_title_without_a_card_level_is_still_read_from_the_title(self, monkeypatch):
        serve(monkeypatch, {("/jobs", 1): page(card(1, title="Senior Software Engineer",
                                                    level=""))})
        (job,) = builtin.fetch("Software Engineer", max_pages=1)
        assert job["experience_level"] == "senior"

    @pytest.mark.parametrize("place", ["2 Locations", "Bengaluru, Karnataka, IND", ""])
    def test_pay_without_a_known_currency_is_left_to_enrichment(self, place):
        # A band stored without its currency would stop the posting page's
        # complete one from ever landing (`enrich_from` takes a band whole).
        found = _builtin_card_details(place, "", "112K-160K Annually", "")
        assert "salary_min" not in found and "salary_currency" not in found

    def test_hourly_pay_keeps_its_period(self):
        found = _builtin_card_details("United States", "", "45-60 Hourly", "")
        assert (found["salary_min"], found["salary_max"], found["salary_period"]) == \
            (45, 60, "hour")


class TestHowFarASearchIsRead:
    def test_pages_are_followed_up_to_the_limit(self, monkeypatch):
        calls = []
        serve(monkeypatch, {("/jobs", n): page(card(n * 10), card(n * 10 + 1))
                            for n in range(1, 10)}, calls)
        jobs = builtin.fetch("Software Engineer", max_pages=3)
        main = [c for c in calls if "/jobs/remote" not in c]
        assert len(main) == 3
        assert len(jobs) == 6

    def test_paging_stops_at_a_page_with_nothing_new(self, monkeypatch):
        calls = []
        # Past its last page the board serves page one again.
        first = page(card(1), card(2))
        serve(monkeypatch, {("/jobs", 1): first, ("/jobs", 2): first,
                            ("/jobs", 3): page(card(3))}, calls)
        jobs = builtin.fetch("Software Engineer", max_pages=5)
        assert [c for c in calls if "/jobs/remote" not in c][-1].endswith("page=2")
        assert {j["source_job_id"] for j in jobs} == {"1", "2"}

    def test_the_remote_search_is_not_cut_short_by_overlapping_the_main_one(self, monkeypatch):
        calls = []
        serve(monkeypatch, {
            ("/jobs", 1): page(card(1), card(2)),
            ("/jobs/remote", 1): page(card(1), card(2)),   # all seen already
            ("/jobs/remote", 2): page(card(3)),
        }, calls)
        jobs = builtin.fetch("Software Engineer", max_pages=3)
        assert {j["source_job_id"] for j in jobs} == {"1", "2", "3"}
        assert len(jobs) == 3

    def test_a_block_stops_the_source(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
            429, text="slow down", request=httpx.Request("GET", url)))
        with pytest.raises(SourceUnavailable):
            builtin.fetch("Software Engineer")


class TestTheSettingsPageControlsIt:
    """The override stored on the profile changes what the fetcher does."""

    def _run(self, db, monkeypatch, overrides):
        from app.services.job_fetcher import _run_all_adapters

        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        profile = db.query(Profile).first()
        calls = []
        serve(monkeypatch, {("/jobs", n): page(card(n)) for n in range(1, 10)}, calls)
        jobs, stats = _run_all_adapters(
            ["Software Engineer"], ["Remote"],
            tunables.effective_settings(profile.data), {}, {}, only={"builtin"},
        )
        return [c for c in calls if "builtin.com/jobs?" in c], stats

    def test_pages_per_search(self, db, monkeypatch):
        main, _ = self._run(db, monkeypatch, {"builtin_max_pages": 1})
        assert len(main) == 1
        db.query(Profile).delete()
        main, _ = self._run(db, monkeypatch, {"builtin_max_pages": 5})
        assert len(main) == 5

    def test_switching_it_off(self, db, monkeypatch):
        calls, stats = self._run(db, monkeypatch, {"builtin_enabled": False})
        assert calls == []
        assert stats["builtin"]["enabled"] is False
