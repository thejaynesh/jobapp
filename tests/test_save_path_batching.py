"""
The fetcher asks which postings are already stored once per chunk
(`deduplication.KnownPostings`), and must answer exactly as the per-posting
lookups did.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import event

from app.models.archived_job import ArchivedJob
from app.models.job import Job
from app.services import job_fetcher
from app.services.deduplication import (
    KnownPostings, compute_dedupe_hash, find_existing_job, ids_by_each_address, was_archived,
)
from tests.test_fetch_task import _make_profile_with_targets, _std_job

NOW = datetime.now(timezone.utc)
LEVER = "https://jobs.lever.co/acme/{}"


def stored(db, **fields):
    url = fields.pop("url")
    job = Job(source=fields.pop("source", "lever"), source_urls=fields.pop("source_urls", [url]),
              url=url, title=fields.pop("title", "Engineer"), company=fields.pop("company", "Acme"),
              location="Remote", fetched_at=NOW,
              dedupe_hash=fields.pop("dedupe_hash", uuid.uuid4().hex), **fields)
    db.add(job)
    db.commit()
    return job


def test_the_chunk_answers_as_the_per_posting_lookups_do(db):
    uid = [str(uuid.uuid4()) for _ in range(6)]
    by_url = stored(db, url=LEVER.format(uid[0]))
    by_canonical = stored(db, url=LEVER.format(uid[1]) + "/apply",
                          source_urls=[LEVER.format(uid[1]) + "/apply", LEVER.format(uid[1])])
    by_id = stored(db, url="https://elsewhere/1", source="greenhouse", source_job_id="77")
    by_hash = stored(db, url="https://elsewhere/2",
                     dedupe_hash=compute_dedupe_hash("Hashco", "Engineer", "Remote"))
    db.add(ArchivedJob(id=uuid.uuid4(), source="lever", source_job_id="gone", url=LEVER.format(uid[4]),
                       source_urls=[LEVER.format(uid[4])], dedupe_hash="tomb", title="t",
                       company="c", fetched_at=NOW))
    db.commit()
    postings = [
        dict(source="simplify", url=LEVER.format(uid[0]), source_job_id=None,
             dedupe_hash="h0"),
        dict(source="simplify", url=LEVER.format(uid[1]), source_job_id=None, dedupe_hash="h1"),
        dict(source="greenhouse", url="https://new/1", source_job_id="77", dedupe_hash="h2"),
        dict(source="lever", url="https://new/2", source_job_id=None,
             dedupe_hash=compute_dedupe_hash("Hashco", "Engineer", "Remote")),
        dict(source="lever", url=LEVER.format(uid[4]), source_job_id=None, dedupe_hash="h4"),
        dict(source="lever", url="https://new/3", source_job_id="x", dedupe_hash="h5",
             apply_url=LEVER.format(uid[1]) + "/apply"),
        dict(source="lever", url="https://nothing/1", source_job_id="y", dedupe_hash="h6"),
    ]
    known = KnownPostings(db, postings)
    for p in postings:
        args = (p["source"], p["url"], p["source_job_id"], p["dedupe_hash"])
        one = find_existing_job(db, *args, apply_url=p.get("apply_url"))
        assert known.existing_id(*args, apply_url=p.get("apply_url")) == (one.id if one else None), p
        assert known.archived(*args, apply_url=p.get("apply_url")) \
            == was_archived(db, *args, apply_url=p.get("apply_url")), p
    assert {by_url.id, by_canonical.id, by_id.id, by_hash.id} <= set(
        known.existing_id(p["source"], p["url"], p["source_job_id"], p["dedupe_hash"],
                          apply_url=p.get("apply_url")) for p in postings)


def test_many_addresses_at_once(db):
    rows = [stored(db, url=f"https://many.example/{i}") for i in range(120)]
    found = ids_by_each_address(db, Job, [r.url for r in rows] + ["https://many.example/none"])
    assert found == {r.url: r.id for r in rows}


def cycle(db, jobs):
    with patch("app.services.query_expansion.expand_search_queries",
               return_value=(["Software Engineer"], None)), \
         patch("app.services.job_fetcher._run_all_adapters",
               side_effect=lambda *a, **k: (list(jobs), {})):
        return job_fetcher.fetch_and_save_jobs(db)


def test_a_posting_listed_twice_in_one_cycle_is_one_row(db):
    _make_profile_with_targets(db)
    uid = uuid.uuid4()
    first = _std_job(source="simplify", source_job_id="s1", url=LEVER.format(uid) + "/apply",
                     title="Engineer New Grad", company="Acme")
    second = _std_job(source="lever", source_job_id=str(uid), url=LEVER.format(uid),
                      title="New Grads - Engineer", company="acme")
    counts = cycle(db, [first, second])
    assert counts["inserted"] == 1 and db.query(Job).count() == 1
    assert db.query(Job).one().seen_by == ["simplify", "lever"]


def test_a_cycle_of_known_postings_asks_the_database_a_handful_of_times(db, monkeypatch):
    _make_profile_with_targets(db)
    jobs = [_std_job(source="lever", source_job_id=f"id{i}", url=f"https://q.example/{i}",
                     title=f"Engineer {i}", description="")
            for i in range(60)]
    cycle(db, jobs)

    statements = []
    engine = db.get_bind().engine
    listener = lambda *args, **kwargs: statements.append(args[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        counts = cycle(db, jobs)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert counts["skipped"] == 60
    lookups = [s for s in statements if "FROM jobs" in s or "FROM archived_jobs" in s]
    # Six questions for the chunk and one read of the matched rows, not
    # three-to-six per posting (240 before).
    assert len(lookups) < 30, len(lookups)


def test_known_urls_looks_at_listing_apply_and_sightings(db):
    stored(db, url="https://a.example/1")
    stored(db, url="https://b.example/1", apply_url="https://apply.example/1")
    stored(db, url="https://c.example/1", source_urls=["https://c.example/1", "https://seen.example/1"])
    asked = {"https://a.example/1", "https://apply.example/1", "https://seen.example/1",
             "https://unknown.example/1"}
    assert job_fetcher._known_urls(db, asked) == asked - {"https://unknown.example/1"}
