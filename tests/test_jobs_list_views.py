"""
The jobs list: its newer filters and sorts, saved views, bulk actions and
keyboard use.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import job_views

NOW = datetime.now(timezone.utc)


def make_job(db, title="Backend Engineer", company="Acme", score=80, **extra):
    job = Job(source="greenhouse", url=f"https://x/{uuid.uuid4()}", source_urls=[], title=title,
              company=company, description=extra.pop("description", "Python services."),
              status=extra.pop("status", JobStatus.matched), llm_score=score,
              fetched_at=extra.pop("fetched_at", NOW - timedelta(hours=1)),
              dedupe_hash=uuid.uuid4().hex, **extra)
    db.add(job)
    db.flush()
    return job


def listed(client, query=""):
    """The job titles on the page, in order."""
    import re

    page = client.get(f"/jobs?{query}").text
    return re.findall(r'href="/jobs/[^"]+/application" class="[^"]*">\s*([^<]+?)\s*</a>', page)


@pytest.fixture
def profile(db):
    db.query(Profile).delete()
    db.add(Profile(data={}))
    db.commit()


class TestNewFilters:
    def test_sponsorship_wording(self, client, db, profile):
        make_job(db, title="Refuses", sponsorship_direction="negative")
        make_job(db, title="Sponsors", sponsorship_direction="positive")
        make_job(db, title="Silent")
        db.commit()
        assert sorted(listed(client, "sponsor=not_no")) == ["Silent", "Sponsors"]
        assert listed(client, "sponsor=yes") == ["Sponsors"]

    def test_hide_closed(self, client, db, profile):
        make_job(db, title="Gone", closed_at=NOW)
        make_job(db, title="Open")
        db.commit()
        assert listed(client, "open_only=1") == ["Open"]

    def test_employment_type_and_years(self, client, db, profile):
        make_job(db, title="Contract", employment_type="contract", required_years=1)
        make_job(db, title="Senior", employment_type="full_time", required_years=8)
        make_job(db, title="Unstated", employment_type="full_time")
        db.commit()
        assert listed(client, "employment_type=contract") == ["Contract"]
        # A posting that states no years is not one that asks too many.
        assert sorted(listed(client, "max_years=3")) == ["Contract", "Unstated"]

    def test_applied_or_not(self, client, db, profile):
        done = make_job(db, title="Applied")
        drafted = make_job(db, title="Drafted")
        make_job(db, title="Untouched")
        db.add_all([Application(job_id=done.id, status=ApplicationStatus.applied),
                    Application(job_id=drafted.id, status=ApplicationStatus.not_applied)])
        db.commit()
        assert listed(client, "applied=yes") == ["Applied"]
        assert sorted(listed(client, "applied=no")) == ["Drafted", "Untouched"]

    def test_company_and_search_in_descriptions(self, client, db, profile):
        make_job(db, title="Engineer A", company="Globex", description="We run Kafka.")
        make_job(db, title="Engineer B", company="Initech", description="We run Postgres.",
                 required_skills=["Kafka Streams"])
        make_job(db, title="Engineer C", company="Hooli")
        db.commit()
        assert listed(client, "company=glob") == ["Engineer A"]
        assert listed(client, "q=kafka") == []
        assert sorted(listed(client, "q=kafka&q_in=all")) == ["Engineer A", "Engineer B"]

    def test_employers_that_file_h1bs(self, client, db, profile):
        make_job(db, title="Files", company="Globex Corp")
        make_job(db, title="Never", company="Tiny Shop")
        db.commit()

        def record(name, db=None):
            if "globex" not in (name or "").lower():
                return None
            return {"certified": 12, "computer": 10, "new_employment": 4, "names": ["GLOBEX CORP"],
                    "more_names": 0, "period": "FY2026 Q1", "needs_sponsorship": False}

        with patch("app.services.sponsorship_history.snapshot", return_value={"totals": {}}), \
                patch("app.services.sponsorship_history.for_company", side_effect=record):
            assert listed(client, "h1b=1") == ["Files"]
        with patch("app.services.sponsorship_history.snapshot", return_value=None):
            page = client.get("/jobs?h1b=1").text
        assert "No H-1B filings are loaded" in page


class TestNewSorts:
    def test_pay(self, client, db, profile):
        make_job(db, title="Low", salary_annual_max=90000)
        make_job(db, title="High", salary_annual_max=200000)
        make_job(db, title="Unpriced")
        db.commit()
        assert listed(client, "sort=salary_desc") == ["High", "Low", "Unpriced"]

    def test_score_with_freshness(self, client, db, profile):
        make_job(db, title="Old 88", score=88, posted_at=NOW - timedelta(days=20))
        make_job(db, title="New 84", score=84, posted_at=NOW - timedelta(hours=2))
        db.commit()
        assert listed(client, "sort=score_desc") == ["Old 88", "New 84"]
        assert listed(client, "sort=fresh_desc") == ["New 84", "Old 88"]

    def test_fewest_missing_skills(self, client, db, profile):
        make_job(db, title="Three missing", missing_skills=["a", "b", "c"])
        make_job(db, title="None missing", missing_skills=[], score=70)
        make_job(db, title="Unscored", score=None, status=JobStatus.new)
        db.commit()
        assert listed(client, "sort=missing_asc") == ["None missing", "Three missing", "Unscored"]

    def test_pages_keep_every_parameter(self, client, db, profile):
        for i in range(51):
            make_job(db, title=f"Job {i}", is_remote=True)
        db.commit()
        page = client.get("/jobs?remote=1&age=all&open_only=1").text
        assert "remote=1" in page and "age=all" in page and "open_only=1" in page and "page=1" in page


class TestSavedViews:
    def test_saving_the_list_as_it_is(self, client, db, profile):
        make_job(db, title="Remote", is_remote=True)
        make_job(db, title="Office")
        db.commit()
        reply = client.post("/jobs/views", data={"name": "Remote only", "query": "remote=1"},
                            follow_redirects=False)
        view = job_views.views(db.query(Profile).first().data)[0]
        assert reply.headers["location"] == f"/jobs?remote=1&view={view['id']}"
        page = client.get("/jobs?remote=1").text
        # Shown with how many jobs it holds, and marked as the one showing.
        assert "Remote only" in page and "border-blue-400 bg-blue-50 text-blue-700" in page

    def test_the_count_is_of_the_view_not_the_page(self, client, db, profile):
        import re

        for _ in range(3):
            make_job(db, is_remote=True)
        make_job(db)
        db.commit()
        job_views.save(db, "Remote", "remote=1")
        page = client.get("/jobs?view=none").text
        assert re.search(r'>Remote</a>\s*<span class="font-semibold">3</span>', page)

    def test_the_default_view_opens_the_page(self, client, db, profile):
        view = job_views.save(db, "Remote", "remote=1")
        client.post(f"/jobs/views/{view['id']}/default")
        reply = client.get("/jobs", follow_redirects=False)
        assert reply.status_code == 303 and reply.headers["location"].startswith("/jobs?remote=1")
        # Everything is one click away, and filters of your own win.
        assert client.get("/jobs?view=none", follow_redirects=False).status_code == 200
        assert client.get("/jobs?status=matched", follow_redirects=False).status_code == 200

    def test_only_one_default_and_it_toggles(self, db, profile):
        a = job_views.save(db, "A", "remote=1")
        b = job_views.save(db, "B", "status=matched")
        job_views.set_default(db, a["id"])
        job_views.set_default(db, b["id"])
        saved = job_views.views(db.query(Profile).first().data)
        assert [v["default"] for v in saved] == [False, True]
        job_views.set_default(db, b["id"])
        assert job_views.default(job_views.views(db.query(Profile).first().data)) is None

    def test_same_name_replaces_and_delete_removes(self, client, db, profile):
        job_views.save(db, "Mine", "remote=1")
        job_views.save(db, "mine", "remote=1&min_score=80")
        saved = job_views.views(db.query(Profile).first().data)
        assert len(saved) == 1 and saved[0]["query"] == "remote=1&min_score=80"
        client.post(f"/jobs/views/{saved[0]['id']}/delete")
        assert job_views.views(db.query(Profile).first().data) == []

    def test_a_nameless_view_is_refused(self, client, db, profile):
        assert client.post("/jobs/views", data={"name": " ", "query": "remote=1"}).status_code == 422


class TestBulkActions:
    def ids(self, *jobs):
        return [str(j.id) for j in jobs]

    def test_star_and_hide_with_a_reason(self, client, db, profile):
        a, b = make_job(db), make_job(db)
        db.commit()
        reply = client.post("/jobs/bulk", data={"job_ids": self.ids(a, b), "action": "star"})
        assert reply.headers.get("HX-Refresh") == "true"
        client.post("/jobs/bulk", data={"job_ids": self.ids(a), "action": "hide",
                                        "reason": "location"})
        db.expire_all()
        assert a.favourite and b.favourite
        assert (a.status, a.filter_reason, a.dismiss_reason) == (
            JobStatus.filtered_out, "manual", "location")

    def test_put_back_only_undoes_your_own_hiding(self, client, db, profile):
        hidden = make_job(db, status=JobStatus.filtered_out, filter_reason="manual",
                          dismiss_reason="pay", dismissed_at=NOW)
        by_matcher = make_job(db, status=JobStatus.filtered_out, filter_reason="low_score")
        db.commit()
        reply = client.post("/jobs/bulk", data={"job_ids": self.ids(hidden, by_matcher),
                                                "action": "restore"})
        assert "1 done, 1 skipped" in reply.text
        db.expire_all()
        assert hidden.status == JobStatus.matched and hidden.dismiss_reason is None
        assert by_matcher.status == JobStatus.filtered_out

    def test_documents_are_queued_once_for_matched_jobs_without_them(self, client, db, profile):
        fresh = make_job(db)
        written = make_job(db)
        filtered_out = make_job(db, status=JobStatus.filtered_out, filter_reason="low_score")
        apps = [Application(job_id=fresh.id), Application(job_id=written.id,
                                                            generation_status="done"),
                Application(job_id=filtered_out.id)]
        db.add_all(apps)
        db.commit()
        with patch("app.tasks.generate.queue_generation", return_value=True) as queued:
            reply = client.post("/jobs/bulk", data={
                "job_ids": self.ids(fresh, written, filtered_out), "action": "generate"})
        assert "1 done, 2 skipped" in reply.text
        assert [str(c.args[0]) for c in queued.call_args_list] == [str(apps[0].id)]
        db.expire_all()
        assert apps[0].generation_status == "generating"

    def test_a_broker_that_refuses_hands_the_slot_back(self, client, db, profile):
        job = make_job(db)
        app_obj = Application(job_id=job.id)
        db.add(app_obj)
        db.commit()
        with patch("app.tasks.generate.queue_generation", return_value=False):
            client.post("/jobs/bulk", data={"job_ids": self.ids(job), "action": "generate"})
        db.expire_all()
        assert app_obj.generation_status == "idle"

    def test_unknown_actions_and_reasons_are_refused(self, client, db, profile):
        job = make_job(db)
        db.commit()
        assert client.post("/jobs/bulk", data={"job_ids": self.ids(job),
                                               "action": "delete"}).status_code == 422
        assert client.post("/jobs/bulk", data={"job_ids": self.ids(job), "action": "hide",
                                               "reason": "bored"}).status_code == 422


sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    from pathlib import Path

    with sync_api.sync_playwright() as playwright:
        launched = None
        for path in [None] + [p for p in ["/opt/pw-browsers/chromium"] if Path(p).exists()]:
            try:
                launched = playwright.chromium.launch(executable_path=path)
                break
            except Exception:  # pragma: no cover — depends on the machine
                continue
        if launched is None:  # pragma: no cover
            pytest.skip("no Chromium here")
        yield launched
        launched.close()


class TestInTheBrowser:
    """The page's own script, on the page the server renders, with requests caught."""

    def open(self, browser, client, db):
        for i in range(3):
            make_job(db, title=f"Job {i}", score=90 - i)
        db.commit()
        html = client.get("/jobs").text
        page = browser.new_page()
        sent = []

        def route(r):
            sent.append((r.request.url, r.request.post_data))
            r.fulfill(status=200, body="<span>ok</span>", content_type="text/html")

        page.route("**/jobs/**", route)
        page.route("**/static/**", lambda r: r.fulfill(status=200, body=""))
        page.set_content(html)
        # The stylesheet is not served here; `hidden` is the one class the
        # script relies on.
        page.add_style_tag(content=".hidden { display: none !important; }")
        return page, sent

    def test_ticking_jobs_shows_the_bar_with_their_ids(self, browser, client, db, profile):
        page, _ = self.open(browser, client, db)
        assert page.is_hidden("#bulk-bar")
        boxes = page.locator("input.job-select")
        boxes.nth(0).check()
        boxes.nth(2).check()
        assert page.is_visible("#bulk-bar") and page.inner_text("#bulk-count") == "2"
        assert page.locator('#bulk-ids input[name="job_ids"]').count() == 2
        page.click("#bulk-clear")
        assert page.is_hidden("#bulk-bar")
        page.close()

    def test_j_k_and_x_move_and_select(self, browser, client, db, profile):
        page, _ = self.open(browser, client, db)
        page.keyboard.press("j")
        page.keyboard.press("j")
        page.keyboard.press("k")
        page.keyboard.press("x")
        cards = page.locator('#job-list > div[id^="job-"]')
        assert "ring-2" in cards.nth(0).get_attribute("class")
        assert cards.nth(0).locator("input.job-select").is_checked()
        assert page.inner_text("#bulk-count") == "1"
        page.close()

    def test_slash_goes_to_search_and_keys_there_are_typing(self, browser, client, db, profile):
        page, _ = self.open(browser, client, db)
        page.keyboard.press("/")
        page.keyboard.type("jx")
        assert page.input_value("#job-search") == "jx"
        assert page.locator("input.job-select:checked").count() == 0
        page.keyboard.press("?")
        assert page.is_hidden("#key-help")
        page.keyboard.press("Escape")
        page.keyboard.press("?")
        assert page.is_visible("#key-help")
        page.close()
