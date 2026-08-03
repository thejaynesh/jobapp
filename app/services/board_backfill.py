"""
One-time mining of the existing jobs table for company ATS boards.

Slug discovery, apply-link resolution and careers-site sniffing all run at fetch
time, on freshly fetched postings only. That leaves the entire back catalogue
untouched — and it's the richest source of boards we have, because every job
ever stored kept its description, and plenty of those descriptions carry an ATS
link that the narrower original patterns couldn't see (the
`greenhouse.io/embed/job_board?for=<slug>` widget above all, plus the various
`api.*` endpoints).

This module re-reads those rows with the current patterns, registers everything
it finds, and optionally follows the aggregator redirects that stored jobs are
still pointing at so they gain a direct apply link too.

Meant to be run once after deploying the registry, via
`python -m app.tasks.backfill` or the Celery task of the same name.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.job import Job

logger = logging.getLogger(__name__)

# Rows per batch. Large enough to amortise the queries, small enough that a
# long-running backfill doesn't hold one enormous result set in memory.
BATCH_SIZE = 500


@dataclass
class BackfillReport:
    jobs_scanned: int = 0
    boards_found: int = 0
    hosts_seen: int = 0
    boards_sniffed: int = 0
    links_attempted: int = 0
    links_resolved: int = 0
    jobs_given_apply_url: int = 0
    per_ats: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "jobs_scanned": self.jobs_scanned,
            "boards_found": self.boards_found,
            "hosts_seen": self.hosts_seen,
            "boards_sniffed": self.boards_sniffed,
            "links_attempted": self.links_attempted,
            "links_resolved": self.links_resolved,
            "jobs_given_apply_url": self.jobs_given_apply_url,
            "per_ats": self.per_ats,
        }


def _iter_job_batches(db: Session, batch_size: int):
    """Page through the jobs table by primary key, oldest first."""
    offset = 0
    while True:
        rows = (
            db.query(Job)
            .order_by(Job.fetched_at.asc(), Job.id.asc())
            .offset(offset)
            .limit(batch_size)
            .all()
        )
        if not rows:
            return
        yield rows
        offset += len(rows)
        if len(rows) < batch_size:
            return


def _job_text(job: Job) -> str:
    return "\n".join(filter(None, (job.url, job.apply_url, job.description)))


def backfill_boards(
    db: Session,
    resolve_links: bool = True,
    sniff_sites: bool = True,
    max_links: int = 2000,
    max_hosts: int = 500,
    workers: int = 8,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
    commit: bool = True,
) -> BackfillReport:
    """
    Mine every stored job for company ATS boards.

    `resolve_links` follows aggregator redirects on stored jobs that never got
    an apply URL; `sniff_sites` inspects the company careers hosts those jobs
    point at. Both make outbound requests, so they're capped and can be turned
    off for a purely offline pass.

    Pass `commit=False` when the caller owns the transaction — the fetch cycle
    runs this inside a savepoint alongside its own bookkeeping.
    """
    from app.services import company_boards as boards
    from app.services.ats_discovery import ALL_ATS, extract_slugs
    from app.services.ats_sniffer import company_host
    from app.services.link_resolver import is_aggregator, is_interstitial

    report = BackfillReport()
    found_total: dict[str, set[str]] = {}
    career_hosts: dict[str, str] = {}
    host_company: dict[str, str] = {}
    unresolved: list[Job] = []

    for batch in _iter_job_batches(db, batch_size):
        for job in batch:
            report.jobs_scanned += 1

            # A job fetched from an ATS is already a board we poll.
            if job.source not in ALL_ATS:
                for ats, slugs in extract_slugs(_job_text(job)).items():
                    found_total.setdefault(ats, set()).update(slugs)

            candidate = job.apply_url or job.url or ""
            if job.source in ALL_ATS or not candidate:
                continue
            if not job.apply_url and is_interstitial(job.url or ""):
                unresolved.append(job)
            elif not is_aggregator(candidate):
                host = company_host(candidate)
                if host:
                    career_hosts.setdefault(host, "")
                    if job.company:
                        host_company.setdefault(host, job.company)

    report.per_ats = {ats: len(slugs) for ats, slugs in found_total.items()}
    report.hosts_seen = len(career_hosts)
    logger.info(
        "board_backfill: scanned %d jobs — %s, %d careers hosts, %d unresolved links",
        report.jobs_scanned, report.per_ats or "no boards", report.hosts_seen,
        len(unresolved),
    )

    if dry_run:
        report.links_attempted = min(len(unresolved), max_links) if resolve_links else 0
        return report

    if found_total:
        report.boards_found = boards.record_boards(
            db, found_total, origin="backfill", revive=False
        )

    if resolve_links and unresolved:
        report.links_attempted, report.links_resolved, report.jobs_given_apply_url = (
            _resolve_stored_links(
                db, unresolved[:max_links], career_hosts, host_company, workers
            )
        )

    if sniff_sites and career_hosts:
        report.boards_sniffed = _sniff_stored_hosts(
            db, career_hosts, host_company, max_hosts, workers
        )

    if commit:
        db.commit()
    logger.info("board_backfill done — %s", report.as_dict())
    return report


def _resolve_stored_links(
    db: Session, jobs: list[Job], career_hosts: dict, host_company: dict,
    workers: int = 8,
) -> tuple[int, int, int]:
    """Follow the aggregator redirects that stored jobs still point at."""
    from app.services import company_boards as boards
    from app.services.ats_discovery import extract_slugs
    from app.services.ats_sniffer import company_host
    from app.services.link_resolver import is_aggregator, resolve_jobs

    # resolve_jobs works on the fetch-time dict shape, so adapt and map back.
    shims = [{"source": job.source, "url": job.url} for job in jobs]
    stats = resolve_jobs(shims, max_links=len(shims), workers=workers)

    given = 0
    found: dict[str, set[str]] = {}
    for job, shim in zip(jobs, shims):
        apply_url = shim.get("apply_url")
        if not apply_url:
            continue
        job.apply_url = apply_url
        given += 1
        for ats, slugs in extract_slugs(apply_url).items():
            found.setdefault(ats, set()).update(slugs)
        if not is_aggregator(apply_url):
            host = company_host(apply_url)
            if host:
                # Reuse the landing page we just downloaded rather than refetching.
                career_hosts.setdefault(host, stats.landing_html.get(job.url, ""))
                if job.company:
                    host_company.setdefault(host, job.company)

    # Mine the landing pages themselves — even one that stopped at another
    # aggregator often embeds the employer's real ATS link.
    for html in stats.landing_html.values():
        for ats, slugs in extract_slugs(html).items():
            found.setdefault(ats, set()).update(slugs)

    if found:
        boards.record_boards(db, found, origin="resolved", revive=False)

    return stats.attempted, stats.resolved, given


def _sniff_stored_hosts(
    db: Session, career_hosts: dict, host_company: dict, max_hosts: int,
    workers: int = 8,
) -> int:
    """Sniff the company careers sites that stored jobs point at."""
    from app.services import company_boards as boards
    from app.services.ats_sniffer import sniff_hosts

    _, _, per_host = sniff_hosts(
        career_hosts, cache=None, max_hosts=max_hosts, workers=workers
    )

    new_boards = 0
    for host, found in per_host.items():
        new_boards += boards.record_boards(
            db, found, origin="sniffed", company=host_company.get(host),
            source_host=host, revive=False,
        )
    return new_boards
