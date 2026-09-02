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


class TestASiteThatWasOpenedAndSentNothing:
    """
    The question the panel could not answer: "did the JobRight crawl get
    anything?"

    Its answer was to omit the site. Every row came from a harvest event, and a
    site that was browsed but forwarded no payloads has none — so it vanished,
    which reads as "never tried" and is the opposite of what happened.

    It is also a different fault from `silent`, with a different fix. Silent
    means payloads arrive and the reader makes nothing of them, which wants a
    recipe. This means no payload arrived: the site is unticked in the
    extension, so the reader is not registered on it, or its pages fetch jobs
    from a URL the interceptor does not forward.
    """

    def browsed(self, db, host, times=1):
        from app.models.agent_event import AgentEvent

        for _ in range(times):
            db.add(AgentEvent(kind="browse", host=host, ok=True,
                              summary={"purpose": "harvest"}))
        db.commit()

    def harvested(self, db, host, found=3, times=1):
        from app.models.agent_event import AgentEvent

        for _ in range(times):
            db.add(AgentEvent(
                kind="harvest", host=host, ok=True,
                summary={"found": found, "inserted": found, "merged": 0},
            ))
        db.commit()

    def rows(self, db):
        from app.services import agent_events

        return {r["host"]: r for r in agent_events.harvest_health(db)}

    def test_it_appears_instead_of_vanishing(self, db):
        self.browsed(db, "jobright.ai", times=12)
        assert "jobright.ai" in self.rows(db)

    def test_it_says_the_pages_were_opened(self, db):
        self.browsed(db, "jobright.ai", times=12)
        row = self.rows(db)["jobright.ai"]
        assert row["pages"] == 12
        assert row["found"] == 0

    def test_its_verdict_is_its_own(self, db):
        # Not "silent", which would send the reader off to write a recipe for
        # payloads that never arrived.
        self.browsed(db, "hiring.cafe", times=12)
        assert self.rows(db)["hiring.cafe"]["verdict"] == "unread"

    def test_a_working_site_keeps_its_page_count(self, db):
        # The denominator, on every row rather than only the broken ones:
        # "0 found" after sixty visits is a different statement from "0 found"
        # after none, and the number was recorded all along without ever being
        # shown beside it.
        self.browsed(db, "my.greenhouse.io", times=5)
        self.harvested(db, "my.greenhouse.io", found=4, times=30)
        row = self.rows(db)["my.greenhouse.io"]
        assert row["verdict"] == "healthy"
        assert row["pages"] == 5

    def test_a_site_that_forwards_but_finds_nothing_is_still_silent(self, db):
        # The distinction this rests on. Payloads arriving and being unreadable
        # is the recipe case, and must not be relabelled.
        self.browsed(db, "otta.com", times=3)
        self.harvested(db, "otta.com", found=0, times=30)
        assert self.rows(db)["otta.com"]["verdict"] == "silent"

    def test_a_site_nobody_touched_is_absent(self, db):
        # Absent still means untried, which is why the browsed case had to stop
        # using it.
        self.browsed(db, "jobright.ai", times=12)
        assert "dice.com" not in self.rows(db)

    def test_it_is_ranked_above_the_working_sites(self, db):
        from app.services import agent_events

        self.browsed(db, "my.greenhouse.io", times=5)
        self.harvested(db, "my.greenhouse.io", found=4, times=30)
        self.browsed(db, "jobright.ai", times=12)

        order = [r["host"] for r in agent_events.harvest_health(db)]
        assert order.index("jobright.ai") < order.index("my.greenhouse.io")

    def test_the_page_says_what_to_do_about_it(self, db, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        self.browsed(db, "jobright.ai", times=12)
        page = client.get("/runs").text
        assert "jobright.ai" in page
        # No agent has reported reading it, so this is the checkbox case.
        # Matched within one line: the sentence wraps in the template, so a
        # phrase spanning the break would fail on the indentation rather than
        # on anything real.
        assert "not switched on for this" in page


class TestTheNumbersAgreeWithTheHeadingAboveThem:
    """
    The page count sits under a heading that says "the last 7 days" and beside
    a table totalling the same events. It was counted over four months, so
    LinkedIn read as 3,435 visits in a week next to a browse total of 854.

    The two windows are still different on purpose — payloads are judged
    against each site's last N responses rather than a stretch of calendar, and
    narrowing that to a week throws the comparison away. Only the plain count
    is bounded, because only the plain count is shown next to other plain
    counts.
    """

    def browsed(self, db, host, times=1, days_ago=0):
        from datetime import timedelta

        from app.models.agent_event import AgentEvent

        for _ in range(times):
            row = AgentEvent(kind="browse", host=host, ok=True, summary={})
            db.add(row)
            db.flush()
            if days_ago:
                row.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        db.commit()

    def rows(self, db, **kwargs):
        from app.services import agent_events

        return {r["host"]: r for r in agent_events.harvest_health(db, **kwargs)}

    def test_pages_outside_the_panel_window_are_not_counted(self, db):
        self.browsed(db, "www.linkedin.com", times=4)
        self.browsed(db, "www.linkedin.com", times=30, days_ago=40)
        assert self.rows(db, pages_days=7)["www.linkedin.com"]["pages"] == 4

    def test_the_summary_passes_its_own_window_through(self, db):
        from app.services import agent_events

        self.browsed(db, "www.linkedin.com", times=3)
        self.browsed(db, "www.linkedin.com", times=50, days_ago=40)
        health = {r["host"]: r
                  for r in agent_events.summary(db, days=7)["harvest_health"]}
        assert health["www.linkedin.com"]["pages"] == 3

    def test_a_site_only_browsed_long_ago_drops_off(self, db):
        # It is not recent activity, so it is not a recent problem either.
        self.browsed(db, "www.dice.com", times=20, days_ago=40)
        assert "www.dice.com" not in self.rows(db, pages_days=7)


class TestSayingWhichFaultItIs:
    """
    "Pages opened, nothing forwarded" is two quite different faults wearing one
    face: a site the reader is not switched on for, and a site it is switched
    on for whose requests it cannot see. The first is a checkbox; the second
    needs a look at the page's network traffic.

    The server cannot tell them apart on its own, so it guessed at both and put
    the reader through a checkbox they may already have ticked. The extension
    knows, and now says.
    """

    def browsed(self, db, host, times=3):
        from app.models.agent_event import AgentEvent

        for _ in range(times):
            db.add(AgentEvent(kind="browse", host=host, ok=True, summary={}))
        db.commit()

    def reading(self, db, hosts):
        from app.models.profile import Profile
        from app.services import browser_tasks

        db.add(Profile(data={}))
        db.commit()
        browser_tasks.record_agent_seen(db, "laptop", ["browse_page"], hosts)

    def row(self, db, host):
        from app.services import agent_events

        return {r["host"]: r for r in agent_events.harvest_health(db)}[host]

    def test_a_site_nobody_reads_is_marked_off(self, db):
        self.browsed(db, "www.dice.com")
        assert self.row(db, "www.dice.com")["enabled"] is False

    def test_a_site_the_reader_is_on_is_marked_on(self, db):
        self.browsed(db, "www.dice.com")
        self.reading(db, ["dice.com"])
        assert self.row(db, "www.dice.com")["enabled"] is True

    def test_a_subdomain_counts_as_the_site(self, db):
        # The extension reports `dice.com`; the browse event records the host
        # the page actually loaded on.
        self.browsed(db, "www.dice.com")
        self.reading(db, ["dice.com"])
        assert self.row(db, "www.dice.com")["enabled"] is True

    def test_reading_one_site_does_not_vouch_for_another(self, db):
        self.browsed(db, "www.dice.com")
        self.reading(db, ["my.greenhouse.io"])
        assert self.row(db, "www.dice.com")["enabled"] is False

    def test_two_browsers_are_unioned(self, db):
        """
        A laptop reading Dice and a desktop that is not means Dice *is* being
        read. Reporting it off because one of them has the box unticked would
        send the user to fix something that is not broken.
        """
        from app.services import browser_tasks

        self.browsed(db, "www.dice.com")
        self.reading(db, ["my.greenhouse.io"])
        browser_tasks.record_agent_seen(db, "desktop", ["browse_page"],
                                        ["dice.com"])
        assert self.row(db, "www.dice.com")["enabled"] is True

    def test_an_older_extension_reporting_nothing_reads_as_off(self, db):
        # Which is the safe way round: it sends the user to a checkbox, and a
        # ticked checkbox costs them a glance rather than an afternoon with
        # DevTools.
        from app.services import browser_tasks

        self.browsed(db, "www.dice.com")
        self.reading(db, [])
        browser_tasks.record_agent_seen(db, "old", ["browse_page"], None)
        assert self.row(db, "www.dice.com")["enabled"] is False

    def test_the_page_says_look_at_the_network_when_it_is_on(self, db, client,
                                                            monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        self.browsed(db, "www.dice.com")
        self.reading(db, ["dice.com"])
        page = client.get("/runs").text
        assert "with the" in page and "reader switched on" in page

    def test_a_working_site_is_never_marked_off(self, db):
        # It forwarded a payload, so it was self-evidently being read. A row
        # arguing with its own evidence would be worse than no row.
        from app.models.agent_event import AgentEvent

        for _ in range(30):
            db.add(AgentEvent(kind="harvest", host="my.greenhouse.io", ok=True,
                              summary={"found": 4, "inserted": 4, "merged": 0}))
        db.commit()
        assert self.row(db, "my.greenhouse.io")["enabled"] is True


class TestWhatTheReaderActuallySaw:
    """
    The denominator. Until now the only evidence of the reader working was a
    payload it forwarded, so a site that opened, scrolled, paginated and
    forwarded nothing reached the server as silence — and silence reads the
    same whether the box is unticked, the page answers in no JSON at all, or
    forty job payloads arrived and every one was filtered out.

    Three faults, three different fixes, one symptom. The reader now reports
    what it looked at whether or not anything got through, which is what tells
    them apart without opening DevTools.
    """

    def browsed(self, db, host, times=3):
        for _ in range(times):
            db.add(AgentEvent(kind="browse", host=host, ok=True, summary={}))
        db.commit()

    def read(self, db, host, *, json=0, sent=0, probed=0, url_no=0,
             first=True, days_ago=0):
        row = AgentEvent(
            kind="read", host=host, ok=True,
            summary={"json": json, "sent": sent, "probed": probed,
                     "url_no": url_no, "shape_no": 0, "first": first},
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(row)
        db.commit()
        return row

    def row(self, db, host, **kwargs):
        return {r["host"]: r
                for r in agent_events.harvest_health(db, **kwargs)}[host]

    def test_a_site_that_only_reported_reads_still_gets_a_row(self, db):
        # Most browsing is the user's own, with no queued browse behind it. A
        # reader that ran and forwarded nothing is the same fault either way.
        self.read(db, "www.dice.com", json=40)
        assert self.row(db, "www.dice.com")["verdict"] == "unread"

    def test_the_counts_reach_the_row(self, db):
        self.browsed(db, "www.dice.com")
        self.read(db, "www.dice.com", json=40, sent=0, probed=2, url_no=38)
        saw = self.row(db, "www.dice.com")["saw"]
        assert (saw["json"], saw["sent"], saw["probed"], saw["url_no"]) == (
            40, 0, 2, 38)

    def test_a_page_reporting_twice_is_counted_once(self, db):
        # The second report carries only what arrived after the first. Adding
        # the totals would have the panel say a site was busier than it is.
        self.read(db, "jobright.ai", json=10, sent=1, first=True)
        self.read(db, "jobright.ai", json=6, sent=1, first=False)
        saw = self.row(db, "jobright.ai")["saw"]
        assert saw["reports"] == 1
        assert (saw["json"], saw["sent"]) == (16, 2)

    def test_an_extension_that_predates_the_flag_still_counts_pages(self, db):
        # Better slightly high than a panel that says "the reader ran on 0
        # pages" directly above the responses it read.
        db.add(AgentEvent(kind="read", host="otta.com", ok=True,
                          summary={"json": 5, "sent": 0}))
        db.commit()
        assert self.row(db, "otta.com")["saw"]["reports"] == 1

    def test_a_reader_that_reported_is_enabled_whatever_the_list_says(self, db):
        # Its own evidence outranks the registration list, which describes the
        # browser's configuration now rather than at the time of the visit.
        self.browsed(db, "www.dice.com")
        self.read(db, "www.dice.com", json=12)
        assert self.row(db, "www.dice.com")["enabled"] is True

    def test_a_site_with_no_reports_carries_no_counts(self, db):
        # None rather than zeros: "never reported" and "reported zero" are
        # different findings and the panel says different things about them.
        self.browsed(db, "hiring.cafe")
        assert self.row(db, "hiring.cafe")["saw"] is None

    def test_reads_outside_the_window_are_not_counted(self, db):
        self.browsed(db, "www.dice.com")
        self.read(db, "www.dice.com", json=40, days_ago=40)
        assert self.row(db, "www.dice.com", pages_days=7)["saw"] is None

    def test_a_working_site_carries_them_too(self, db):
        for _ in range(30):
            db.add(AgentEvent(kind="harvest", host="my.greenhouse.io", ok=True,
                              summary={"found": 4, "inserted": 4, "merged": 0}))
        db.commit()
        self.read(db, "my.greenhouse.io", json=30, sent=30)
        row = self.row(db, "my.greenhouse.io")
        assert row["verdict"] == "healthy"
        assert row["saw"]["sent"] == 30

    def test_reads_do_not_invent_harvest_yield(self, db):
        self.read(db, "www.dice.com", json=40)
        assert self.row(db, "www.dice.com")["found"] == 0


class TestTheThreeFaultsReadDifferentlyOnThePage:
    def setup_agent(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")

    def read(self, db, host, **counts):
        db.add(AgentEvent(kind="browse", host=host, ok=True, summary={}))
        db.add(AgentEvent(kind="read", host=host, ok=True,
                          summary={"first": True, **counts}))
        db.commit()

    def test_no_json_at_all_says_widening_the_filter_cannot_help(
            self, db, client, monkeypatch):
        # The honest answer, and the one that saves the most time: its listings
        # are not fetched over an API this can observe.
        self.setup_agent(monkeypatch)
        self.read(db, "www.dice.com", json=0, sent=0, probed=0, url_no=0)
        page = client.get("/runs").text
        assert "saw no JSON responses at" in page

    def test_json_seen_and_none_forwarded_points_at_the_payloads(
            self, db, client, monkeypatch):
        # The opposite finding: the data is arriving, so what is missing is a
        # match rather than access.
        self.setup_agent(monkeypatch)
        self.read(db, "www.dice.com", json=40, sent=0, probed=0, url_no=40)
        page = client.get("/runs").text
        assert "arriving over an API" in page

    def test_nothing_reported_at_all_points_at_the_extension(
            self, db, client, monkeypatch):
        # Registered but never running: the host permission was granted and the
        # script did not inject.
        from app.services import browser_tasks
        from app.models.profile import Profile

        self.setup_agent(monkeypatch)
        db.add(Profile(data={}))
        db.commit()
        browser_tasks.record_agent_seen(db, "laptop", ["browse_page"],
                                        ["dice.com"])
        for _ in range(3):
            db.add(AgentEvent(kind="browse", host="www.dice.com", ok=True,
                              summary={}))
        db.commit()
        page = client.get("/runs").text
        assert "never reported back" in page


class TestABoardAskedOverItsOwnApi:
    """
    A sweep's silence means the opposite of the reader's silence.

    The passive reader only runs when a page fetches something, so it reporting
    nothing means the page was quiet. A sweep *asks*, so it reporting nothing
    means the ask failed — and it fails for three unrelated reasons: the site
    never handed over a token, the API refused the one it did, or the board
    genuinely had nothing new. All three reach this table as zero payloads and
    all three want a different fix, so the reason it stopped is the field the
    whole event exists for.
    """

    def sweep(self, db, host, *, ok=True, pages=0, rows=0, limit=20,
              stopped="end of list", detail="", status=0, minutes_ago=0):
        row = AgentEvent(
            kind="sweep", host=host, ok=ok,
            summary={"pages": pages, "rows": rows, "limit": limit,
                     "stopped": stopped, "status": status, "detail": detail},
            created_at=datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago),
        )
        db.add(row)
        db.commit()
        return row

    def only(self, db, **kwargs):
        rows = agent_events.sweep_stats(db, **kwargs)
        assert len(rows) == 1, rows
        return rows[0]

    def test_a_host_nobody_swept_is_absent(self, db):
        assert agent_events.sweep_stats(db) == []

    def test_what_it_fetched_is_totalled(self, db):
        self.sweep(db, "dashboard.tsenta.com", pages=12, rows=240)
        self.sweep(db, "dashboard.tsenta.com", pages=8, rows=160)
        row = self.only(db)
        assert (row["runs"], row["pages"], row["records"]) == (2, 20, 400)

    def test_the_reason_is_the_last_one_not_a_summary(self, db):
        # "What is wrong with it now" is the useful question. A host that
        # failed twice yesterday and worked this morning is working.
        self.sweep(db, "dashboard.tsenta.com", ok=False, stopped="no token",
                   minutes_ago=90)
        self.sweep(db, "dashboard.tsenta.com", pages=9, rows=180,
                   stopped="short page")
        row = self.only(db)
        assert row["last_ok"] is True
        assert row["stopped"] == "short page"
        assert row["reason"] == "reached the end of the list"

    def test_a_refused_token_says_to_sign_in_again(self, db):
        self.sweep(db, "dashboard.tsenta.com", ok=False,
                   stopped="not signed in", status=401)
        row = self.only(db)
        assert row["last_ok"] is False
        assert "sign in" in row["reason"]

    def test_no_token_at_all_is_a_different_sentence(self, db):
        # The two sign-in failures are not the same fault: one is the site
        # never answering, the other is the API rejecting what it answered
        # with, and only the second means the session itself expired.
        self.sweep(db, "dashboard.tsenta.com", ok=False, stopped="no token")
        assert "never handed over a token" in self.only(db)["reason"]

    def test_a_budget_stop_says_there_is_more(self, db):
        self.sweep(db, "dashboard.tsenta.com", pages=40, rows=4000,
                   stopped="page budget")
        assert "probably more" in self.only(db)["reason"]

    def test_an_unanticipated_failure_is_shown_as_reported(self, db):
        # Rounding an unknown reason to the nearest known one would be exactly
        # the sort of diagnostic that sends you looking in the wrong place.
        self.sweep(db, "dashboard.tsenta.com", ok=False, stopped="HTTP 503")
        assert self.only(db)["reason"] == "HTTP 503"

    def test_the_page_size_that_worked_is_kept(self, db):
        # The only record of whether asking for more than the site's own
        # client uses was honoured.
        self.sweep(db, "dashboard.tsenta.com", pages=4, rows=400, limit=100)
        assert self.only(db)["limit"] == 100

    def test_sweeps_outside_the_window_are_not_counted(self, db):
        self.sweep(db, "dashboard.tsenta.com", pages=5, minutes_ago=60 * 24 * 9)
        assert agent_events.sweep_stats(db, days=7) == []

    def test_the_panel_carries_them(self, db):
        self.sweep(db, "dashboard.tsenta.com", pages=6, rows=120)
        assert agent_events.summary(db)["sweeps"][0]["pages"] == 6

    def test_a_sweep_is_not_a_harvest(self, db):
        # It reports what was asked for; the payloads it forwards are counted
        # by the harvest events they become, and counting both would double.
        self.sweep(db, "dashboard.tsenta.com", pages=6, rows=120)
        assert agent_events.harvest_yield(db)["found"] == 0
