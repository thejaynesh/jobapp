"""
Everything the system did over a window, arranged to argue about what to fix.

Written for the question "we let it run for two days, now what should we work
on" — and shaped by how badly the last few of those went. Handshake looked
unreadable for a day because `found: 0` meant two different things; JobRight
got four wrong diagnoses because the numbers being compared came from
different pipelines. Both were failures of *reporting*, not of the system, and
both would have been visible in an hour with a page like this.

So the bias here is towards rates and denominators rather than totals. Ten
thousand jobs with no description is not a better outcome than five hundred
complete ones, and a source's row count says nothing about which it is.

Read-only. Stores nothing, changes nothing.

    docker compose -f docker-compose.prod.yml run --rm web python scripts/review.py
    docker compose -f docker-compose.prod.yml run --rm web python scripts/review.py 48

The argument is the window in hours, defaulting to 48. Sections:

  1. intake          what arrived, from where
  2. completeness    how much of each job is actually filled in
  3. funnel          where jobs stop
  4. matching        what scoring did, and what it cost
  5. browser tier    what the extension saw and what became of it
  6. overlap         how much the sources agree, which is how much they waste
  7. trouble         everything that failed
"""

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

WIDTH = 104


def rule(title: str) -> None:
    print()
    print(f"=== {title} " + "=" * max(0, WIDTH - len(title) - 5))


def pct(part: int, whole: int) -> str:
    if not whole:
        return "    -"
    return f"{100.0 * part / whole:>4.0f}%"


def main():
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import Float, case, func

    from app.database import SessionLocal
    from app.models.agent_event import AgentEvent
    from app.models.job import Job, JobStatus
    from app.models.llm_call import LLMCall

    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 48
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    db = SessionLocal()
    try:
        window = Job.fetched_at >= since

        # --- 1. intake ---------------------------------------------------
        rule(f"1. intake over the last {hours}h")
        total = db.query(func.count(Job.id)).filter(window).scalar() or 0
        ever = db.query(func.count(Job.id)).scalar() or 0
        print(f"{total:,} jobs arrived in the window; {ever:,} in the table.")
        print()
        rows = (
            db.query(Job.source, func.count(Job.id), func.max(Job.fetched_at))
            .filter(window).group_by(Job.source)
            .order_by(func.count(Job.id).desc()).all()
        )
        print(f"{'source':<26} {'jobs':>8} {'share':>7}   newest")
        for source, count, newest in rows:
            print(f"{source:<26} {count:>8,} {pct(count, total):>7}   "
                  f"{newest:%Y-%m-%d %H:%M}")

        # A source that used to produce and has stopped is the most actionable
        # line in this whole report, and a table of what *did* arrive can never
        # show it.
        live = {source for source, _c, _n in rows}
        dormant = (
            db.query(Job.source, func.count(Job.id), func.max(Job.fetched_at))
            .filter(~Job.source.in_(live or {""}))
            .group_by(Job.source).order_by(func.max(Job.fetched_at).desc()).all()
        )
        if dormant:
            print()
            print("Silent in this window, but has produced before:")
            for source, count, newest in dormant:
                print(f"{source:<26} {count:>8,} {'':>7}   last {newest:%Y-%m-%d %H:%M}")

        # --- 2. completeness ---------------------------------------------
        rule("2. how complete each source's jobs are")
        print("Priority one is full, correct, structured data, and this is the")
        print("measure of it. A source high in section 1 and low here is making")
        print("work for the enricher rather than saving it.")
        print()

        def filled(column):
            return func.sum(case((column.isnot(None), 1), else_=0))

        long_enough = func.sum(
            case((func.length(func.coalesce(Job.description, "")) > 400, 1), else_=0)
        )
        detail = (
            db.query(
                Job.source,
                func.count(Job.id),
                long_enough,
                filled(Job.salary_min),
                filled(Job.location),
                filled(Job.experience_level),
                filled(Job.required_years),
                filled(Job.details_extracted_at),
            )
            .filter(window).group_by(Job.source)
            .order_by(func.count(Job.id).desc()).all()
        )
        print(f"{'source':<26} {'jobs':>7} {'descr':>6} {'salary':>6} {'loc':>6} "
              f"{'level':>6} {'years':>6} {'parsed':>6}")
        for src, n, desc, sal, loc, lvl, yrs, det in detail:
            print(f"{src:<26} {n:>7,} {pct(desc or 0, n)} {pct(sal or 0, n)} "
                  f"{pct(loc or 0, n)} {pct(lvl or 0, n)} {pct(yrs or 0, n)} "
                  f"{pct(det or 0, n)}")
        print()
        print("descr = description over 400 chars. A short one is a teaser, and")
        print("matching on a teaser is the most expensive mistake in the system:")
        print("it spends a model call to reach a confident wrong answer.")

        # --- 3. funnel ----------------------------------------------------
        rule("3. where jobs stop")
        for status in JobStatus:
            n = (db.query(func.count(Job.id))
                   .filter(window, Job.status == status).scalar() or 0)
            print(f"{status.value:<26} {n:>8,} {pct(n, total)}")
        print()
        scored = (db.query(func.count(Job.id))
                    .filter(window, Job.llm_score.isnot(None)).scalar() or 0)
        deep = (db.query(func.count(Job.id))
                  .filter(window, Job.llm_score_deep.isnot(None)).scalar() or 0)
        print(f"{'scored (llm_score)':<26} {scored:>8,} {pct(scored, total)}")
        print(f"{'second opinion':<26} {deep:>8,} {pct(deep, total)}")
        unscored = total - scored
        if unscored > 0:
            print()
            print(f"{unscored:,} jobs arrived and were never scored. Either the")
            print("budget ran out, they were filtered before scoring, or the")
            print("matcher never reached them — section 4 and section 7 say which.")

        # --- 4. matching ---------------------------------------------------
        rule("4. matching")
        bands = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]
        print("score distribution, of those scored:")
        for low, high in bands:
            n = (db.query(func.count(Job.id))
                   .filter(window, Job.llm_score >= low, Job.llm_score < high)
                   .scalar() or 0)
            bar = "#" * min(60, int(60 * n / scored)) if scored else ""
            print(f"  {low:>3}-{high - 1:<3} {n:>7,} {pct(n, scored)}  {bar}")
        print()
        print("A distribution piled into one band is the finding here. Everything")
        print("at 40-60 means the matcher is not discriminating, and no amount of")
        print("extra intake improves a ranking that cannot separate.")

        by_matcher = (
            db.query(Job.matched_by, func.count(Job.id),
                     func.avg(Job.llm_score.cast(Float)))
            .filter(window, Job.matched_by.isnot(None))
            .group_by(Job.matched_by).order_by(func.count(Job.id).desc()).all()
        )
        if by_matcher:
            print()
            print(f"{'matched by':<26} {'jobs':>8} {'mean score':>11}")
            for matcher, n, mean in by_matcher:
                print(f"{matcher or '(none)':<26} {n:>8,} "
                      f"{(mean or 0):>11.1f}")

        calls = (
            db.query(LLMCall.stage, func.count(LLMCall.id),
                     func.sum(case((LLMCall.ok, 0), else_=1)),
                     func.sum(func.coalesce(LLMCall.prompt_tokens, 0)),
                     func.sum(func.coalesce(LLMCall.completion_tokens, 0)),
                     func.avg(LLMCall.duration_ms))
            .filter(LLMCall.created_at >= since)
            .group_by(LLMCall.stage)
            .order_by(func.count(LLMCall.id).desc()).all()
        )
        if calls:
            print()
            print(f"{'llm stage':<26} {'calls':>8} {'failed':>7} {'prompt tok':>12} "
                  f"{'out tok':>10} {'mean ms':>9}")
            for stage, n, bad, ptok, ctok, ms in calls:
                print(f"{stage:<26} {n:>8,} {bad or 0:>7,} {ptok or 0:>12,} "
                      f"{ctok or 0:>10,} {(ms or 0):>9,.0f}")

        # --- 5. browser tier ------------------------------------------------
        rule("5. what the browser saw")
        reads = (
            db.query(
                AgentEvent.host,
                func.count(AgentEvent.id),
                func.sum(func.coalesce(
                    AgentEvent.summary["json"].astext.cast(Float), 0)),
                func.sum(func.coalesce(
                    AgentEvent.summary["sent"].astext.cast(Float), 0)),
                func.sum(func.coalesce(
                    AgentEvent.summary["probed"].astext.cast(Float), 0)),
            )
            .filter(AgentEvent.kind == "read", AgentEvent.created_at >= since)
            .group_by(AgentEvent.host)
            .order_by(func.count(AgentEvent.id).desc()).limit(20).all()
        )
        if reads:
            print(f"{'host':<34} {'reports':>8} {'json seen':>10} "
                  f"{'forwarded':>10} {'probed':>8}")
            for host, n, seen, sent, probed in reads:
                print(f"{(host or '(none)'):<34} {n:>8,} {seen or 0:>10,.0f} "
                      f"{sent or 0:>10,.0f} {probed or 0:>8,.0f}")
            print()
            print("json seen at zero means the listings never arrive as a payload")
            print("the reader can observe, and no filter change will ever help.")

        harvests = (
            db.query(
                AgentEvent.host,
                func.count(AgentEvent.id),
                func.sum(func.coalesce(
                    AgentEvent.summary["found"].astext.cast(Float), 0)),
                func.sum(func.coalesce(
                    AgentEvent.summary["inserted"].astext.cast(Float), 0)),
                func.sum(func.coalesce(
                    AgentEvent.summary["no_url"].astext.cast(Float), 0)),
                func.sum(func.coalesce(
                    AgentEvent.summary["no_company"].astext.cast(Float), 0)),
            )
            .filter(AgentEvent.kind == "harvest", AgentEvent.ok.is_(True),
                    AgentEvent.created_at >= since)
            .group_by(AgentEvent.host)
            .order_by(func.count(AgentEvent.id).desc()).limit(20).all()
        )
        if harvests:
            print()
            print(f"{'host':<34} {'payloads':>9} {'found':>8} {'inserted':>9} "
                  f"{'no url':>8} {'no co.':>8}")
            for host, n, found, ins, nourl, noco in harvests:
                print(f"{(host or '(none)'):<34} {n:>9,} {found or 0:>8,.0f} "
                      f"{ins or 0:>9,.0f} {nourl or 0:>8,.0f} {noco or 0:>8,.0f}")
            print()
            print("A row with payloads and no found, but a large `no url`, is a")
            print("board whose postings were all recognised and all thrown away —")
            print("it wants an entry in `_POSTING_URL`, not a better reader.")

        # --- 6. overlap ------------------------------------------------------
        rule("6. how much the sources overlap")
        merged = (db.query(func.count(Job.id))
                    .filter(window, func.array_length(Job.source_urls, 1) > 1)
                    .scalar() or 0)
        print(f"{merged:,} of {total:,} jobs ({pct(merged, total)}) carry more than")
        print("one source URL, meaning two sources found the same posting and the")
        print("dedupe merged them.")
        print()
        print("Read this in both directions. High overlap means a new source is")
        print("mostly re-finding what you already had, and effort is better spent")
        print("on depth than on another board. Near-zero overlap across boards")
        print("that ought to share postings is more likely a dedupe that is not")
        print("catching them, which shows up as duplicate rows rather than reach.")

        # --- 7. trouble -------------------------------------------------------
        rule("7. what failed")
        bad = (
            db.query(AgentEvent.kind, AgentEvent.host, func.count(AgentEvent.id))
            .filter(AgentEvent.ok.is_(False), AgentEvent.created_at >= since)
            .group_by(AgentEvent.kind, AgentEvent.host)
            .order_by(func.count(AgentEvent.id).desc()).limit(20).all()
        )
        if bad:
            print(f"{'kind':<16} {'host':<40} {'count':>8}")
            for kind, host, n in bad:
                print(f"{kind:<16} {(host or '(none)'):<40} {n:>8,}")
        else:
            print("No failed agent events.")

        errors = (
            db.query(LLMCall.stage, LLMCall.error, func.count(LLMCall.id))
            .filter(LLMCall.ok.is_(False), LLMCall.created_at >= since)
            .group_by(LLMCall.stage, LLMCall.error)
            .order_by(func.count(LLMCall.id).desc()).limit(10).all()
        )
        if errors:
            print()
            print("failed model calls:")
            for stage, error, n in errors:
                print(f"  {n:>6,}  {stage:<20} {(error or '')[:60]}")

        challenges = (
            db.query(AgentEvent.host, func.count(AgentEvent.id))
            .filter(AgentEvent.kind == "browse",
                    AgentEvent.created_at >= since,
                    AgentEvent.summary["challenge"].astext.isnot(None))
            .group_by(AgentEvent.host)
            .order_by(func.count(AgentEvent.id).desc()).limit(10).all()
        )
        if challenges:
            print()
            print("hosts that asked for a human check:")
            for host, n in challenges:
                print(f"  {n:>6,}  {host}")

        rule("retention")
        # Whether this report is looking at the whole window or only at what
        # survived the pruner. Reading a truncated window as a real one is how
        # a quiet source gets mistaken for a dead one.
        #
        # The test is the row count against the cap, not the age of the oldest
        # row. "Nothing older than the window exists" and "everything older
        # than the window was deleted" look identical from the timestamp alone,
        # and on a system that has only just started they are the opposite
        # conclusion — so a warning based on age alone would cry wolf on the
        # first run and be ignored on the run that mattered.
        from app.config import settings

        for label, model, cap in (
            ("agent events", AgentEvent,
             int(getattr(settings, "AGENT_EVENT_KEEP_ROWS", 20000))),
            ("model calls", LLMCall,
             int(getattr(settings, "LLM_LOG_KEEP_ROWS", 2000))),
        ):
            held = db.query(func.count(model.id)).scalar() or 0
            oldest = db.query(func.min(model.created_at)).scalar()
            note = ""
            if cap > 0 and held >= cap * 0.95:
                note = (f"  ** at {pct(held, cap).strip()} of the {cap:,} cap — "
                        f"older rows are being deleted,\n     so this report is "
                        f"probably missing the start of the window. Raise it.")
            print(f"{label:<14} {held:>9,} rows, cap {cap:>9,}, oldest "
                  f"{oldest or '(none)'}")
            if note:
                print(note)
    finally:
        db.close()


if __name__ == "__main__":
    main()
