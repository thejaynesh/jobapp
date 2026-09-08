"""
The nodes the reader recognised as jobs and then threw away, and why.

`replay_samples.py` asks whether a payload contains anything job-shaped. This
asks the question after that one: the reader *did* recognise these as jobs, and
returned none of them. Between `_looks_like_job` saying yes and `extract_jobs`
returning a row there are two rejections, and both are silent.

Handshake is the case this was written for. Two hundred and sixty-five
title-bearing objects, a hundred and fifty of them job-shaped, and a live
harvest that reported `found: 0` a hundred and thirty-six times — because
`_looks_like_job` accepts an id *or* a URL, and `_normalize` then requires a
URL and reconstructs one only for LinkedIn. Every Handshake posting carries an
id and no URL, so every one of them was read, recognised, and dropped. The same
mistake cost the whole of Greenhouse's aggregate board once already, which is
what the `publicUrl` comment in `harvest.py` is about.

It is the *silence* that makes this expensive rather than the rule. A board
losing every job for want of a URL reports exactly what a board with no jobs in
it reports, so the investigation starts from "the reader cannot read this
payload" and goes looking in the wrong place — for days, in this instance.

Read-only. Stores nothing, changes nothing.

    docker compose -f docker-compose.prod.yml run --rm web \\
        python scripts/why_zero.py app.joinhandshake.com
"""

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

SHOW_KEYS = 18
SHOW_EXAMPLES = 3


def main():
    from app.database import SessionLocal
    from app.models.harvest_recipe import HarvestSample
    from app.services import harvest
    from app.services.harvest import extract_jobs, source_for_url

    host = sys.argv[1] if len(sys.argv) > 1 else "app.joinhandshake.com"
    db = SessionLocal()
    try:
        samples = (
            db.query(HarvestSample)
            .filter(HarvestSample.host == host)
            .order_by(HarvestSample.bytes.desc())
            .limit(40)
            .all()
        )
        if not samples:
            sys.exit(f"No samples stored for {host}.")

        source = source_for_url(samples[0].source_url or f"https://{host}/")
        print(f"host {host}, filed as {source}")
        print()

        recognised = 0
        kept = 0
        no_company = 0
        no_url = 0
        keys = collections.Counter()
        examples = []

        for sample in samples:
            # The source the live path would have passed, not the default. That
            # argument is the difference between 151 jobs and none, and reading
            # the samples without it is how the discrepancy stayed hidden.
            kept += len(extract_jobs(sample.payload, source=source))

            for node, company in harvest._walk_scoped(sample.payload):
                if not harvest._looks_like_job(node, company=company):
                    continue
                recognised += 1
                if harvest._normalize(node, source=source, company=company):
                    continue
                # Recognised as a job and refused. There are exactly two ways
                # out of `_normalize`, so saying which one is the whole
                # diagnosis.
                if not (harvest._first(node, harvest._COMPANY_KEYS) or company):
                    no_company += 1
                    continue
                no_url += 1
                keys.update(k for k in node)
                if len(examples) < SHOW_EXAMPLES:
                    examples.append(node)

        print(f"recognised as a job : {recognised}")
        print(f"returned by the reader: {kept}")
        print(f"dropped, no company : {no_company}")
        print(f"dropped, no URL     : {no_url}")
        print()

        if not no_url:
            print("Nothing is being lost for want of a URL.")
            return

        print("A job with an id and no URL is reconstructed only for LinkedIn "
              "— see\n`_normalize`. For every other source it is dropped, and "
              "this is that count.")
        print()
        print(f"--- keys on the dropped nodes " + "-" * 48)
        for key, count in keys.most_common(SHOW_KEYS):
            print(f"    {count:>6}  {key}")

        print()
        print(f"--- {SHOW_EXAMPLES} of them in full, to write the URL template from "
              + "-" * 18)
        print("    (redact anything personal before pasting)")
        for node in examples:
            print()
            for key, value in list(node.items())[:20]:
                shown = value
                if isinstance(shown, str):
                    shown = shown[:70]
                elif isinstance(shown, (dict, list)):
                    shown = f"<{type(shown).__name__} of {len(shown)}>"
                print(f"    {key:<26} {shown!r}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
