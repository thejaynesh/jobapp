"""
Choosing which interview writeups are worth reading.

Ingestion is the easy half and barely tested here. What is tested is the
ranking, because that is where the value is and where being subtly wrong is
invisible: a corpus that returns forty reports in a plausible-looking order is
indistinguishable from one that returns them in a useful order.
"""

from datetime import datetime, timedelta, timezone

from app.models.interview_report import InterviewReport
from app.services import interview_corpus as corpus

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def report(db=None, **kwargs):
    data = {
        "company": kwargs.pop("company", "Acme Corp"),
        "source": kwargs.pop("source", "reddit"),
        "url": kwargs.pop("url", "https://reddit.com/r/leetcode/comments/1"),
        "title": kwargs.pop("title", "Acme interview experience"),
        "body": kwargs.pop("body", "Five rounds. " * 50),
        "posted_at": kwargs.pop("posted_at", NOW - timedelta(days=30)),
        "role_hint": kwargs.pop("role_hint", None),
    }
    data.update(kwargs)
    if db is not None:
        corpus.ingest(db, [data])
    return data


class TestRecencyWeight:
    def test_a_recent_report_carries_full_weight(self):
        assert corpus.recency_weight(NOW - timedelta(days=30), NOW) == 1.0

    def test_weight_is_flat_across_the_first_six_months(self):
        # March and June describe the same loop; ranking between them would be
        # ranking on noise.
        early = corpus.recency_weight(NOW - timedelta(days=10), NOW)
        late = corpus.recency_weight(NOW - timedelta(days=175), NOW)
        assert early == late == 1.0

    def test_weight_decays_after_that(self):
        year_old = corpus.recency_weight(NOW - timedelta(days=365), NOW)
        assert 0 < year_old < 1.0

    def test_older_is_always_worth_less(self):
        assert corpus.recency_weight(NOW - timedelta(days=400), NOW) > corpus.recency_weight(
            NOW - timedelta(days=700), NOW
        )

    def test_very_old_reports_are_worthless(self):
        assert corpus.recency_weight(NOW - timedelta(days=365 * 4), NOW) == 0.0

    def test_a_future_date_is_treated_as_current_not_infinite(self):
        # A date in the future is a parsing bug upstream, not a report from
        # tomorrow's interview.
        assert corpus.recency_weight(NOW + timedelta(days=30), NOW) == 1.0

    def test_a_naive_datetime_is_assumed_utc_rather_than_crashing(self):
        naive = datetime(2026, 7, 1)
        assert corpus.recency_weight(naive, NOW) > 0


class TestLevelDetection:
    def test_reads_common_new_grad_phrasings(self):
        for text in ("SDE-1 University Grad", "new grad role", "campus hire", "L3"):
            assert corpus.level_of(text) == "new_grad", text

    def test_reads_intern(self):
        assert corpus.level_of("Summer Internship 2026") == "intern"

    def test_reads_senior(self):
        assert corpus.level_of("Senior Software Engineer") == "senior"

    def test_says_nothing_when_the_text_says_nothing(self):
        assert corpus.level_of("Interview experience") is None
        assert corpus.level_of("") is None


class TestScoring:
    def test_recent_beats_old(self):
        recent = InterviewReport(**report(posted_at=NOW - timedelta(days=20)), company_key="acme")
        old = InterviewReport(**report(posted_at=NOW - timedelta(days=800)), company_key="acme")
        assert corpus.score(recent, now=NOW) > corpus.score(old, now=NOW)

    def test_a_matching_level_is_worth_more(self):
        matching = InterviewReport(**report(role_hint="SDE-1"), company_key="acme")
        unstated = InterviewReport(**report(role_hint=None), company_key="acme")
        assert corpus.score(matching, "new_grad", NOW) > corpus.score(unstated, "new_grad", NOW)

    def test_a_different_level_is_worth_less(self):
        # A senior loop and a new grad loop are different processes, so a
        # senior report is worse than one that does not say.
        senior = InterviewReport(**report(role_hint="Senior Engineer"), company_key="acme")
        unstated = InterviewReport(**report(role_hint=None), company_key="acme")
        assert corpus.score(senior, "new_grad", NOW) < corpus.score(unstated, "new_grad", NOW)

    def test_substance_counts_a_little(self):
        full = InterviewReport(**report(body="Detailed writeup. " * 200), company_key="acme")
        thin = InterviewReport(**report(body="rejected"), company_key="acme")
        assert corpus.score(full, now=NOW) > corpus.score(thin, now=NOW)

    def test_but_length_never_outweighs_recency(self):
        # A rambling post from 2022 must not outrank a short one from last month.
        long_old = InterviewReport(
            **report(body="words " * 5000, posted_at=NOW - timedelta(days=900)),
            company_key="acme",
        )
        short_new = InterviewReport(
            **report(body="Three rounds, OA then two onsites.", posted_at=NOW - timedelta(days=20)),
            company_key="acme",
        )
        assert corpus.score(short_new, now=NOW) > corpus.score(long_old, now=NOW)


class TestIngest:
    def test_stores_a_usable_report(self, db):
        counts = corpus.ingest(db, [report()])
        assert counts["stored"] == 1
        stored = db.query(InterviewReport).one()
        assert stored.company_key == "acme"

    def test_refuses_an_undated_report(self, db):
        # It cannot be placed on the recency scale, and recency is the ranking.
        counts = corpus.ingest(db, [report(posted_at=None)])
        assert counts["undated"] == 1
        assert db.query(InterviewReport).count() == 0

    def test_undated_is_counted_separately_from_invalid(self, db):
        # A source that stops supplying dates has broken; one supplying junk has
        # broken differently, and the counts have to tell them apart.
        counts = corpus.ingest(db, [report(posted_at=None), report(url="", company="")])
        assert counts["undated"] == 1
        assert counts["invalid"] == 1

    def test_the_same_url_is_not_stored_twice(self, db):
        corpus.ingest(db, [report()])
        counts = corpus.ingest(db, [report()])
        assert counts["duplicate"] == 1
        assert db.query(InterviewReport).count() == 1

    def test_an_unknown_source_is_refused(self, db):
        assert corpus.ingest(db, [report(source="hearsay")])["invalid"] == 1

    def test_company_names_are_normalized_to_one_key(self, db):
        corpus.ingest(db, [report(company="Acme, Inc.", url="https://x/1")])
        corpus.ingest(db, [report(company="Acme Inc", url="https://x/2")])
        keys = {r.company_key for r in db.query(InterviewReport).all()}
        assert len(keys) == 1


class TestRetrieval:
    def test_returns_reports_for_the_company(self, db):
        report(db)
        assert len(corpus.reports_for(db, "Acme Corp", now=NOW)) == 1

    def test_matches_a_differently_written_company_name(self, db):
        report(db, company="Acme, Inc.")
        assert len(corpus.reports_for(db, "Acme Inc", now=NOW)) == 1

    def test_another_companys_reports_are_not_returned(self, db):
        report(db, company="Globex")
        assert corpus.reports_for(db, "Acme Corp", now=NOW) == []

    def test_best_first(self, db):
        report(db, url="https://x/old", posted_at=NOW - timedelta(days=700))
        report(db, url="https://x/new", posted_at=NOW - timedelta(days=10))
        found = corpus.reports_for(db, "Acme Corp", now=NOW)
        assert found[0].url.endswith("/new")

    def test_stale_reports_are_excluded_when_there_is_something_fresher(self, db):
        report(db, url="https://x/ancient", posted_at=NOW - timedelta(days=365 * 5))
        report(db, url="https://x/recent", posted_at=NOW - timedelta(days=10))
        found = corpus.reports_for(db, "Acme Corp", now=NOW)
        assert [r.url for r in found] == ["https://x/recent"]

    def test_but_stale_reports_come_back_when_there_is_nothing_else(self, db):
        # Old information about a company still beats none, and returning an
        # empty list while holding something would be the wrong kind of strict.
        report(db, url="https://x/ancient", posted_at=NOW - timedelta(days=365 * 5))
        assert len(corpus.reports_for(db, "Acme Corp", now=NOW)) == 1

    def test_an_unknown_company_returns_nothing(self, db):
        assert corpus.reports_for(db, "Nobody Ltd", now=NOW) == []

    def test_an_empty_company_returns_nothing(self, db):
        report(db)
        assert corpus.reports_for(db, "", now=NOW) == []

    def test_the_limit_is_respected(self, db):
        for n in range(6):
            report(db, url=f"https://x/{n}")
        assert len(corpus.reports_for(db, "Acme Corp", limit=3, now=NOW)) == 3


class TestCoverage:
    def test_reports_what_is_held_per_source(self, db):
        report(db, url="https://x/1", source="reddit")
        report(db, url="https://x/2", source="github")
        stats = corpus.coverage(db)
        assert stats["total"] == 2
        assert stats["by_source"]["reddit"]["count"] == 1

    def test_every_source_appears_even_at_zero(self, db):
        # A source sitting at zero is the thing worth noticing; leaving it out
        # of the report is how it goes unnoticed.
        report(db)
        assert set(corpus.coverage(db)["by_source"]) == {"geeksforgeeks", "reddit", "github"}

    def test_an_empty_corpus_reports_zeroes(self, db):
        assert corpus.coverage(db)["total"] == 0

    def test_can_be_scoped_to_one_company(self, db):
        report(db, company="Acme Corp", url="https://x/1")
        report(db, company="Globex", url="https://x/2")
        assert corpus.coverage(db, "Globex")["total"] == 1
