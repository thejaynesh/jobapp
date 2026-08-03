import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

from app.models.company_board import CompanyBoard
from app.models.job import Job, JobStatus
from app.services.board_backfill import backfill_boards

_NOW = datetime.now(timezone.utc)


def _job(db, *, url="https://ex.com/1", description="", source="adzuna",
         company="Acme", title="SWE", apply_url=None) -> Job:
    job = Job(
        source=source,
        source_job_id=None,
        source_urls=[url],
        title=title,
        company=company,
        location="NYC",
        is_remote=False,
        url=url,
        apply_url=apply_url,
        description=description,
        experience_level="mid",
        status=JobStatus.new,
        fetched_at=_NOW,
        # Per-URL so fixtures never collide on the unique dedupe_hash.
        dedupe_hash=hashlib.sha256(url.encode()).hexdigest()[:32],
    )
    db.add(job)
    db.flush()
    return job


def _offline(db, **kwargs):
    """Backfill with both network passes disabled."""
    return backfill_boards(db, resolve_links=False, sniff_sites=False, **kwargs)


class TestSlugMining:
    def test_recovers_a_board_from_a_stored_description(self, db):
        _job(db, description="Apply: https://boards.greenhouse.io/legacyco/jobs/1")
        report = _offline(db)
        assert report.jobs_scanned == 1
        assert db.query(CompanyBoard).filter(
            CompanyBoard.ats == "greenhouse", CompanyBoard.slug == "legacyco"
        ).count() == 1

    def test_recovers_the_embed_form_the_old_patterns_missed(self, db):
        """The whole point of the backfill: these were dropped at fetch time."""
        _job(db, description=(
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=embedco">'
        ))
        _offline(db)
        assert db.query(CompanyBoard).filter(
            CompanyBoard.slug == "embedco"
        ).count() == 1

    def test_mines_the_stored_url_and_apply_url_too(self, db):
        _job(db, url="https://jobs.lever.co/urlco/1", source="jsearch")
        _job(db, url="https://www.adzuna.com/land/ad/2",
             apply_url="https://jobs.ashbyhq.com/applyco/x")
        _offline(db)
        slugs = {b.slug for b in db.query(CompanyBoard).all()}
        assert {"urlco", "applyco"} <= slugs

    def test_jobs_already_from_an_ats_are_skipped(self, db):
        _job(db, source="greenhouse", url="https://boards.greenhouse.io/self/jobs/1")
        _offline(db)
        assert db.query(CompanyBoard).count() == 0

    def test_boards_are_marked_as_backfilled(self, db):
        _job(db, description="https://jobs.lever.co/originco/1")
        _offline(db)
        assert db.query(CompanyBoard).one().origin == "backfill"

    def test_does_not_revive_a_retired_board(self, db):
        """Stored text is history, not evidence the company is still hiring."""
        from app.services.company_boards import record_boards
        record_boards(db, {"lever": ["retiredco"]}, origin="discovered")
        board = db.query(CompanyBoard).one()
        board.active = False
        db.flush()

        _job(db, description="https://jobs.lever.co/retiredco/1")
        _offline(db)
        db.refresh(board)
        assert board.active is False

    def test_reports_counts_per_ats(self, db):
        _job(db, description="https://jobs.lever.co/alpha/1 https://jobs.lever.co/beta/2")
        _job(db, url="https://boards.greenhouse.io/gamma/jobs/3", source="jsearch")
        report = _offline(db)
        assert report.per_ats == {"lever": 2, "greenhouse": 1}
        assert report.boards_found == 3

    def test_pages_through_more_jobs_than_one_batch(self, db):
        for i in range(7):
            _job(db, url=f"https://ex.com/{i}", title=f"Role {i}",
                 description=f"https://jobs.lever.co/co{i}/1")
        report = _offline(db, batch_size=2)
        assert report.jobs_scanned == 7
        assert db.query(CompanyBoard).count() == 7

    def test_empty_database_is_a_no_op(self, db):
        report = _offline(db)
        assert report.jobs_scanned == 0
        assert report.boards_found == 0

    def test_dry_run_writes_nothing(self, db):
        _job(db, description="https://jobs.lever.co/ghostco/1")
        report = _offline(db, dry_run=True)
        assert report.per_ats == {"lever": 1}
        assert db.query(CompanyBoard).count() == 0


class TestLinkResolution:
    def test_stored_aggregator_jobs_gain_an_apply_url(self, db):
        job = _job(db, url="https://www.adzuna.com/land/ad/5")

        def _resolve(shims, **kwargs):
            from app.services.link_resolver import ResolveStats
            shims[0]["apply_url"] = "https://boards.greenhouse.io/foundco/jobs/9"
            return ResolveStats(attempted=1, resolved=1)

        with patch("app.services.link_resolver.resolve_jobs", side_effect=_resolve):
            report = backfill_boards(db, sniff_sites=False)

        db.refresh(job)
        assert job.apply_url == "https://boards.greenhouse.io/foundco/jobs/9"
        assert report.jobs_given_apply_url == 1
        assert db.query(CompanyBoard).filter(
            CompanyBoard.slug == "foundco"
        ).count() == 1

    def test_jobs_that_already_have_an_apply_url_are_left_alone(self, db):
        _job(db, url="https://www.adzuna.com/land/ad/6",
             apply_url="https://jobs.lever.co/done/1")
        with patch("app.services.link_resolver.resolve_jobs") as resolve:
            backfill_boards(db, sniff_sites=False)
        resolve.assert_not_called()

    def test_non_aggregator_urls_are_not_resolved(self, db):
        _job(db, url="https://careers.acme.com/job/1", source="remotive")
        with patch("app.services.link_resolver.resolve_jobs") as resolve:
            backfill_boards(db, sniff_sites=False)
        resolve.assert_not_called()

    def test_landing_pages_are_mined_even_when_the_link_dead_ends(self, db):
        """Stopping at another aggregator still often exposes the ATS link."""
        job = _job(db, url="https://www.adzuna.com/land/ad/7")

        def _resolve(shims, **kwargs):
            from app.services.link_resolver import ResolveStats
            return ResolveStats(
                attempted=1, resolved=1,
                landing_html={job.url: '<a href="https://jobs.lever.co/buriedco/1">apply</a>'},
            )

        with patch("app.services.link_resolver.resolve_jobs", side_effect=_resolve):
            backfill_boards(db, sniff_sites=False)

        assert db.query(CompanyBoard).filter(
            CompanyBoard.slug == "buriedco"
        ).count() == 1

    def test_respects_the_link_budget(self, db):
        for i in range(5):
            _job(db, url=f"https://www.adzuna.com/land/ad/{i}", title=f"Role {i}")

        captured = {}

        def _resolve(shims, **kwargs):
            from app.services.link_resolver import ResolveStats
            captured["count"] = len(shims)
            return ResolveStats(attempted=len(shims))

        with patch("app.services.link_resolver.resolve_jobs", side_effect=_resolve):
            backfill_boards(db, sniff_sites=False, max_links=2)
        assert captured["count"] == 2


class TestCareerSiteSniffing:
    def test_sniffs_hosts_stored_jobs_point_at(self, db):
        _job(db, url="https://careers.storedco.com/job/1", source="remotive")

        with patch("app.services.ats_sniffer.sniff_host",
                   return_value={"ashby": ["storedco"]}) as sniff:
            report = backfill_boards(db, resolve_links=False)

        assert sniff.call_args[0][0] == "careers.storedco.com"
        assert report.boards_sniffed == 1
        board = db.query(CompanyBoard).filter(CompanyBoard.slug == "storedco").one()
        assert board.origin == "sniffed"
        assert board.source_host == "careers.storedco.com"
        assert board.company == "Acme"

    def test_each_host_is_sniffed_once_however_many_jobs_share_it(self, db):
        for i in range(4):
            _job(db, url=f"https://careers.shared.com/job/{i}", title=f"Role {i}",
                 source="remotive")
        with patch("app.services.ats_sniffer.sniff_host", return_value={}) as sniff:
            report = backfill_boards(db, resolve_links=False)
        assert sniff.call_count == 1
        assert report.hosts_seen == 1

    def test_aggregator_hosts_are_never_sniffed(self, db):
        _job(db, url="https://www.indeed.com/viewjob?jk=1", source="indeed")
        with patch("app.services.ats_sniffer.sniff_host") as sniff:
            backfill_boards(db, resolve_links=False)
        sniff.assert_not_called()

    def test_offline_mode_makes_no_requests_at_all(self, db):
        _job(db, url="https://www.adzuna.com/land/ad/8")
        _job(db, url="https://careers.acme.com/job/1", source="remotive", title="Other")
        with patch("app.services.link_resolver.resolve_jobs") as resolve, \
             patch("app.services.ats_sniffer.sniff_host") as sniff:
            _offline(db)
        resolve.assert_not_called()
        sniff.assert_not_called()
