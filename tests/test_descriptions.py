"""
Canonical description cleaning.

The rule under test everywhere: whatever shape a source sends, what reaches
`Job.description` is plain readable text — or nothing at all. "Nothing at all"
is a real answer here: a challenge page stored as a description is worse than
an empty column, because the filter counts its skills and the model reads it as
if it were the job.
"""

import uuid
from datetime import datetime, timezone

from app.models.job import Job, JobStatus
from app.services.descriptions import clean, looks_like_block_page


class TestUnescaping:
    def test_escaped_html_is_unwrapped(self):
        raw = "&lt;p&gt;We need &lt;strong&gt;Python&lt;/strong&gt;&lt;/p&gt;"
        assert clean(raw) == "We need Python"

    def test_double_escaped_html_is_unwrapped(self):
        # Greenhouse escapes its HTML; an aggregator that re-serializes the
        # payload escapes it again.
        assert clean("&amp;lt;div&amp;gt;Hello&amp;lt;/div&amp;gt;") == "Hello"

    def test_a_literal_ampersand_survives(self):
        # The reason unescaping stops at two passes: a third would eat this.
        assert clean("R&amp;D team") == "R&D team"

    def test_non_breaking_spaces_become_spaces(self):
        # The skill counter splits on whitespace, and "\xa0" is not whitespace
        # to str.split the way a space is.
        assert clean("Python&nbsp;and&nbsp;Go") == "Python and Go"


class TestStructure:
    def test_list_items_become_bullet_lines(self):
        raw = "<ul><li>5 years Python</li><li>AWS</li></ul>"
        assert clean(raw) == "- 5 years Python\n- AWS"

    def test_paragraphs_become_blank_line_separated(self):
        assert clean("<p>One</p><p>Two</p>") == "One\n\nTwo"

    def test_script_and_style_contents_are_dropped(self):
        raw = "<style>.a{color:red}</style><script>var x=1;</script><p>Real</p>"
        assert clean(raw) == "Real"

    def test_runs_of_blank_lines_collapse(self):
        assert clean("One\n\n\n\n\nTwo") == "One\n\nTwo"

    def test_runs_of_spaces_collapse(self):
        assert clean("One     two\t\tthree") == "One two three"

    def test_empty_bullets_are_dropped(self):
        # A <li> holding only an image leaves a dash and nothing else.
        assert clean("<ul><li><img src='x'></li><li>Real</li></ul>") == "- Real"

    def test_plain_text_is_left_alone(self):
        text = "Senior Engineer\n\nYou will build things.\n- Python\n- Go"
        assert clean(text) == text

    def test_a_less_than_sign_in_prose_is_not_a_tag(self):
        # Nothing here is HTML, so nothing should be stripped as if it were.
        assert clean("Latency <10ms at p99") == "Latency <10ms at p99"

    def test_malformed_markup_still_yields_text(self):
        assert "Hello" in clean("<p>Hello<//p><<>>")


class TestBlockPages:
    CHALLENGE = (
        "Verify you are human by completing the action below. "
        "example.com needs to review the security of your connection. "
        "Ray ID: 8ab. Performance & security by Cloudflare"
    )

    def test_a_challenge_page_cleans_to_nothing(self):
        assert clean(self.CHALLENGE) == ""

    def test_a_challenge_page_is_recognised(self):
        assert looks_like_block_page(self.CHALLENGE) is True

    def test_a_real_posting_that_mentions_cloudflare_survives(self):
        # An infrastructure job legitimately names the thing. Length is what
        # separates the two, and throwing this away is the worse error.
        posting = (
            "We are hiring a platform engineer. " * 100
            + "You will operate our Cloudflare edge."
        )
        assert clean(posting).endswith("Cloudflare edge.")
        assert looks_like_block_page(posting) is False

    def test_empty_input_is_not_a_block_page(self):
        assert looks_like_block_page("") is False
        assert looks_like_block_page(None) is False


class TestIdempotence:
    """
    Every write path calls clean(), and some call it after another already
    did. Cleaning twice has to mean the same as cleaning once, or the second
    call silently rewrites text the first one settled.
    """

    CASES = [
        "&lt;ul&gt;&lt;li&gt;A&lt;/li&gt;&lt;/ul&gt;",
        "<p>Hello</p><br><p>World</p>",
        "Plain text already",
        "R&amp;D and Q&amp;A",
        "",
        None,
    ]

    def test_cleaning_twice_changes_nothing(self):
        for case in self.CASES:
            once = clean(case)
            assert clean(once) == once, case


def _job(**kwargs) -> Job:
    defaults = dict(
        source="test",
        source_urls=["https://example.com/1"],
        title="Backend Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/1",
        status=JobStatus.new,
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
    )
    defaults.update(kwargs)
    return Job(**defaults)


class TestMergeCleansBeforeComparing:
    """
    Raw markup inflates a description's length by roughly a third, so the
    cross-post merge has to measure cleaned text against cleaned text — or an
    HTML copy wins on the tape measure and replaces better text with worse.
    """

    def test_an_html_crosspost_does_not_replace_longer_plain_text(self, db):
        from app.services.deduplication import merge_or_skip

        plain = "We need a backend engineer with Python and Go experience here."
        job = _job(description=plain)
        db.add(job)
        db.commit()

        # Longer raw, shorter once the tags come off — which is the whole trap.
        html_version = (
            "<div><section><p><strong>We need a backend engineer.</strong>"
            "</p></section></div>"
        )
        assert len(html_version) > len(plain)
        from app.services.descriptions import clean as _clean
        assert len(_clean(html_version)) < len(plain)

        merge_or_skip(db, job, "https://example.com/2", html_version, layer=3)
        assert job.description == plain

    def test_a_genuinely_fuller_crosspost_wins_and_is_cleaned(self, db):
        from app.services.deduplication import merge_or_skip

        job = _job(description="Short.")
        db.add(job)
        db.commit()

        merge_or_skip(
            db, job, "https://example.com/2",
            "<p>Much longer description of the role.</p><ul><li>Python</li></ul>",
            layer=3,
        )
        assert job.description == "Much longer description of the role.\n\n- Python"
        assert "<" not in job.description


class TestWritePathsClean:
    """
    Cleaning lives at the door, not in each adapter — otherwise a new source
    reintroduces HTML soup by forgetting to call it.
    """

    def test_harvested_jobs_are_stored_as_plain_text(self, db):
        from app.services.harvest import save_harvested_jobs

        save_harvested_jobs(db, [{
            "title": "Backend Engineer",
            "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/999/",
            "source_job_id": "999",
            "description": "<p>Build things.</p><ul><li>Python</li></ul>",
        }])

        job = db.query(Job).filter(Job.source_job_id == "999").one()
        assert job.description == "Build things.\n\n- Python"

    def test_a_harvested_block_page_stores_no_description(self, db):
        from app.services.harvest import save_harvested_jobs

        save_harvested_jobs(db, [{
            "title": "Backend Engineer",
            "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/998/",
            "source_job_id": "998",
            "description": "Please enable JavaScript and cookies to continue",
        }])

        job = db.query(Job).filter(Job.source_job_id == "998").one()
        assert job.description is None


class TestBackfill:
    def test_html_rows_are_rewritten_in_place(self, db):
        from app.tasks.descriptions import clean_descriptions

        job = _job(description="<p>Hello</p><ul><li>Python</li></ul>")
        db.add(job)
        db.commit()

        counts = clean_descriptions(db, batch_size=10)
        db.refresh(job)

        assert counts["cleaned"] == 1
        assert job.description == "Hello\n\n- Python"

    def test_a_stored_block_page_becomes_no_description(self, db):
        from app.tasks.descriptions import clean_descriptions

        job = _job(
            description="<p>Verify you are human. Performance by Cloudflare</p>"
        )
        db.add(job)
        db.commit()

        counts = clean_descriptions(db, batch_size=10)
        db.refresh(job)

        assert counts["emptied"] == 1
        assert job.description is None

    def test_cleaning_does_not_stamp_description_updated_at(self, db):
        """
        Reformatting is not new information. Stamping here would tell the user
        to rewrite the documents for every job they have.
        """
        from app.tasks.descriptions import clean_descriptions

        job = _job(description="<p>Hello there</p>")
        db.add(job)
        db.commit()

        clean_descriptions(db, batch_size=10)
        db.refresh(job)

        assert job.description_updated_at is None

    def test_plain_text_rows_are_left_alone(self, db):
        from app.tasks.descriptions import clean_descriptions

        job = _job(description="Already plain, nothing to do.")
        db.add(job)
        db.commit()

        counts = clean_descriptions(db, batch_size=10)
        assert counts["scanned"] == 0

    def test_dry_run_writes_nothing(self, db):
        from app.tasks.descriptions import clean_descriptions

        job = _job(description="<p>Hello</p>")
        db.add(job)
        db.commit()

        counts = clean_descriptions(db, batch_size=10, dry_run=True)
        assert counts["cleaned"] == 1

        db.expire_all()
        assert db.query(Job).filter(Job.id == job.id).one().description == "<p>Hello</p>"

    def test_batches_advance_rather_than_repeating(self, db):
        """
        Paginated by id, so a row that still trips the pattern after cleaning
        cannot be picked up forever.
        """
        from app.tasks.descriptions import clean_descriptions

        for i in range(7):
            db.add(_job(description=f"<p>Job {i}</p>",
                        url=f"https://example.com/{i}",
                        source_urls=[f"https://example.com/{i}"]))
        db.commit()

        counts = clean_descriptions(db, batch_size=2)
        assert counts["scanned"] == 7
        assert counts["cleaned"] == 7
