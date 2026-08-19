"""
Noticing that a site stopped working.

The harvest reads each page's own API responses rather than its markup, which
survives redesigns that would break a CSS selector. What it does not survive is
a payload renaming every field at once — and when that happens the extension
keeps running, keeps forwarding, and finds nothing. The only symptom is a
source quietly contributing less, which nobody notices for weeks.

The hard part is that a zero is not the signal. Browsing a feed forwards plenty
of responses that legitimately contain no jobs, so `found == 0` happens many
times a day on a perfectly healthy site. So these tests are mostly about *not*
crying wolf: the alarm fires on a site that used to yield and has enough recent
traffic to judge, and stays quiet for every other shape of zero.

The comparison is counted, not dated — each site against its own last N
forwarded payloads. A calendar split said nothing until a week of history
existed and then called a site you used heavily on Monday "not browsed lately".
Several tests below pin that: history from a fortnight ago still counts as the
site's current state if nothing has happened since.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent_event import AgentEvent
from app.services import agent_events


def event(db, host, *, found=0, days_ago=1, inserted=0, merged=0):
    row = AgentEvent(
        kind="harvest",
        host=host,
        agent_id="laptop",
        ok=True,
        summary={"found": found, "inserted": inserted, "merged": merged,
                 "source": f"{host.split('.')[0]}_harvest"},
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(row)
    return row


def many(db, host, count, *, found=0, days_ago=1):
    for _ in range(count):
        event(db, host, found=found, days_ago=days_ago)


def verdicts(db, **kwargs):
    return {row["host"]: row["verdict"] for row in
            agent_events.harvest_health(db, **kwargs)}


class TestAWorkingSite:
    def test_a_site_finding_jobs_is_healthy(self, db):
        many(db, "www.linkedin.com", 5, found=3, days_ago=1)
        db.commit()

        assert verdicts(db)["www.linkedin.com"] == "healthy"

    def test_one_job_in_the_recent_half_is_enough(self, db):
        # A site that found something is working, however much noise came with
        # it. Nothing here should demand a yield rate.
        many(db, "www.indeed.com", 40, found=0, days_ago=2)
        event(db, "www.indeed.com", found=1, days_ago=2)
        db.commit()

        assert verdicts(db)["www.indeed.com"] == "healthy"

    def test_it_reports_what_the_site_contributed(self, db):
        event(db, "www.linkedin.com", found=9, inserted=4, merged=2, days_ago=1)
        db.commit()

        row = agent_events.harvest_health(db)[0]
        assert (row["found"], row["inserted"], row["merged"]) == (9, 4, 2)


class TestTheAlarm:
    def test_a_site_that_stopped_finding_jobs_is_flagged(self, db):
        # The case this exists for: it was working, the traffic is still
        # arriving, and nothing job-shaped comes out any more.
        many(db, "www.linkedin.com", 10, found=5, days_ago=12)
        many(db, "www.linkedin.com", 30, found=0, days_ago=2)
        db.commit()

        assert verdicts(db)["www.linkedin.com"] == "regressed"

    def test_a_site_that_never_yielded_is_not_called_a_regression(self, db):
        # Different problem, different fix: this one needs field aliases, not
        # a look at what changed.
        many(db, "otta.com", 30, found=0, days_ago=2)
        db.commit()

        assert verdicts(db)["otta.com"] == "silent"

    def test_the_last_time_it_found_anything_is_reported(self, db):
        many(db, "www.linkedin.com", 10, found=5, days_ago=12)
        many(db, "www.linkedin.com", 30, found=0, days_ago=2)
        db.commit()

        row = agent_events.harvest_health(db)[0]
        assert row["last_found_at"] is not None
        assert row["earlier_found"] == 50


class TestItDoesNotCryWolf:
    def test_a_site_you_stopped_browsing_still_reads_as_working(self, db):
        # The single most likely false positive under a calendar split: you had
        # a busy fortnight and did not open Glassdoor. Nothing about that says
        # the reader broke — the last thing known about the site is that it
        # worked, and that is what it should say.
        many(db, "www.glassdoor.com", 10, found=5, days_ago=12)
        db.commit()

        assert verdicts(db)["www.glassdoor.com"] == "healthy"

    def test_a_couple_of_stray_page_loads_are_not_enough_to_judge(self, db):
        # Below the traffic floor, "found nothing" is as likely to mean you
        # opened the homepage as that the reader broke.
        many(db, "www.dice.com", 4, found=0, days_ago=2)
        db.commit()

        assert verdicts(db)["www.dice.com"] == "quiet"

    def test_an_empty_history_reports_nothing_at_all(self, db):
        assert agent_events.harvest_health(db) == []

    def test_events_that_are_not_harvests_are_ignored(self, db):
        db.add(AgentEvent(kind="autofill", host="www.linkedin.com", ok=True,
                          summary={"filled": 3}))
        db.commit()

        assert agent_events.harvest_health(db) == []

    def test_events_older_than_the_outer_bound_are_ignored(self, db):
        many(db, "www.linkedin.com", 30, found=5, days_ago=200)
        db.commit()

        assert agent_events.harvest_health(db) == []


class TestItJudgesByPayloadsNotByCalendar:
    """
    Each site against its own last N responses, not against a stretch of days.

    A calendar split could say nothing at all until a week of history existed,
    which made the panel useless on the day it shipped — and then it called a
    site used heavily on Monday and not since "not browsed lately".
    """

    def test_one_afternoon_of_browsing_is_a_complete_verdict(self, db):
        # The point of the change: no waiting for a baseline to accumulate.
        # Everything here happened within a few hours today.
        many(db, "www.linkedin.com", 20, found=6, days_ago=0)
        many(db, "www.linkedin.com", 30, found=0, days_ago=0)
        db.commit()

        assert verdicts(db)["www.linkedin.com"] == "regressed"

    def test_a_fortnight_old_baseline_still_counts_as_the_before(self, db):
        # Nothing has happened on this site since, so its last known state is
        # its current state however long ago that was.
        many(db, "otta.com", 20, found=4, days_ago=40)
        many(db, "otta.com", 30, found=0, days_ago=1)
        db.commit()

        assert verdicts(db)["otta.com"] == "regressed"

    def test_a_site_is_judged_on_its_own_traffic_not_the_busiest_one(self, db):
        # A site opened twice a month must not be declared broken for being
        # quieter than LinkedIn.
        many(db, "www.linkedin.com", 60, found=3, days_ago=1)
        many(db, "wellfound.com", 6, found=2, days_ago=30)
        db.commit()

        assert verdicts(db)["wellfound.com"] == "healthy"

    def test_only_the_recent_window_decides_healthy(self, db):
        # Enough recent zeros to push every yielding payload out of the recent
        # window: the old jobs are the comparison, not the verdict.
        many(db, "www.indeed.com", 10, found=7, days_ago=3)
        many(db, "www.indeed.com", 25, found=0, days_ago=1)
        db.commit()

        row = agent_events.harvest_health(db)[0]
        assert row["verdict"] == "regressed"
        assert row["found"] == 0
        assert row["earlier_found"] == 70

    def test_history_beyond_two_windows_is_not_compared_against(self, db):
        # Fifty payloads back is history, not a before-and-after.
        many(db, "monster.com", 40, found=9, days_ago=30)
        many(db, "monster.com", 50, found=0, days_ago=1)
        db.commit()

        row = agent_events.harvest_health(db)[0]
        assert row["verdict"] == "silent"
        assert row["earlier_found"] == 0


class TestOrdering:
    def test_problems_come_first(self, db):
        # The panel is read to find a problem. A regression buried under four
        # working sites is a regression nobody sees.
        many(db, "healthy.com", 5, found=9, days_ago=1)
        many(db, "regressed.com", 10, found=4, days_ago=12)
        many(db, "regressed.com", 30, found=0, days_ago=2)
        many(db, "silent.com", 30, found=0, days_ago=2)
        db.commit()

        order = [row["host"] for row in agent_events.harvest_health(db)]
        assert order[0] == "regressed.com"
        assert order.index("silent.com") < order.index("healthy.com")

    def test_every_row_carries_a_readable_label(self, db):
        many(db, "www.linkedin.com", 5, found=3, days_ago=1)
        db.commit()

        row = agent_events.harvest_health(db)[0]
        assert row["label"] == agent_events.HEALTH_LABELS[row["verdict"]]


class TestOnTheRunsPage:
    def test_the_panel_shows_each_site(self, client, db):
        many(db, "www.linkedin.com", 5, found=3, days_ago=1)
        db.commit()

        body = client.get("/runs").text
        assert "Harvest by site" in body
        assert "www.linkedin.com" in body

    def test_a_regression_says_what_to_do(self, client, db):
        many(db, "www.linkedin.com", 10, found=4, days_ago=12)
        many(db, "www.linkedin.com", 30, found=0, days_ago=2)
        db.commit()

        body = client.get("/runs").text
        assert "Stopped finding jobs" in body
        assert "docs/HARVEST.md" in body

    def test_the_page_still_renders_with_no_harvests(self, client, db):
        assert client.get("/runs").status_code == 200

    def test_the_summary_includes_it(self, db):
        many(db, "www.linkedin.com", 5, found=3, days_ago=1)
        db.commit()

        assert "harvest_health" in agent_events.summary(db)


class TestTheSiteList:
    """The extension list and the server's source names have to agree."""

    def _extension_hosts(self):
        import re

        source = open("extension/sites.js").read()
        # The host out of each match pattern: "https://*.dice.com/*" -> dice.com
        return {
            re.sub(r"^\*\.", "", host)
            for host in re.findall(r'"https://([^/"]+)/\*"', source)
        }

    def test_every_harvested_host_has_its_own_source_name(self, db):
        # A host the extension harvests but the server does not name still
        # works — the extractor never looks at the host — but its yield lands
        # in LinkedIn's bucket where it cannot be judged separately.
        from app.services.harvest import HARVEST_SOURCES

        known = set(HARVEST_SOURCES)
        missing = {
            host for host in self._extension_hosts()
            if not any(host == d or host.endswith(f".{d}") for d in known)
        }
        assert missing == set(), f"no source name for: {sorted(missing)}"

    def test_the_new_sites_are_actually_registered(self, db):
        hosts = self._extension_hosts()
        assert {"dice.com", "ziprecruiter.com", "wellfound.com"} <= hosts

    def test_storage_keys_are_unique(self):
        import re

        source = open("extension/sites.js").read()
        keys = re.findall(r'storageKey:\s*"([^"]+)"', source)
        assert len(keys) == len(set(keys))

    def test_the_options_page_renders_the_list_rather_than_hardcoding_it(self):
        # Three copies of this list is how a site ends up registered and
        # permissioned with no way to turn it on.
        html = open("extension/options.html").read()
        assert 'id="harvest-sites"' in html
        assert 'id="harvestIndeed"' not in html
        assert 'type="module"' in html
