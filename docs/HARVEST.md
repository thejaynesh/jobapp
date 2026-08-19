# Harvesting jobs from pages you browse

While you browse a job site normally, the extension reads the job data the page
already received and posts it to your server. No extra requests are made, so
there is nothing to rate-limit and nothing to detect: the traffic is a person
using the site.

This is how postings arrive from sites that answer the server with a challenge
— LinkedIn, Indeed and Glassdoor all do — and it is the only way descriptions
arrive for those at all.

## You do not need doc IDs, query IDs, or any API keys

This is worth saying up front because it is the usual assumption.

Reading LinkedIn's API *actively* would mean calling Voyager yourself, which
means a `queryId`/doc ID per endpoint, a CSRF token, and a session cookie — and
all three rotate, so it would break every few weeks and need re-harvesting by
hand.

This does none of that. `extension/interceptor.js` runs in the page's own
JavaScript world and wraps `fetch` and `XMLHttpRequest`. When the page asks its
own API for job cards, the response comes back and the wrapper reads a *copy*
of it on the way past. Whatever doc ID the page used, it used it — we never
have to know what it was.

The consequence: **there is nothing to configure per site, and nothing to keep
up to date when a site rotates its identifiers.**

## Why it survives redesigns

The obvious way to read jobs off a page is CSS selectors. That is the wrong
layer: class names are regenerated on every redesign, so selector-based
extraction rots on someone else's schedule — and it rots silently, because a
changed class name yields zero jobs, which looks exactly like an empty page.

So `app/services/harvest.py` never looks at paths or hosts. It walks the entire
JSON payload and picks out any object that *looks* like a job: something with a
title, a company, and either an id or a URL. Field names are matched against a
list of aliases (`_TITLE_KEYS`, `_COMPANY_KEYS`, and so on).

A redesign that moves the nesting around keeps working. So does a new site
nobody wrote a parser for — which is why adding one is a single line.

## Adding a site

1. Append a row to `HARVEST_SITES` in `extension/sites.js`:

   ```js
   {
     id: "example",
     label: "Example Jobs",
     storageKey: "harvestExample",
     matches: ["https://*.example.com/*"],
   }
   ```

   `storageKey` must be unique. `matches` is both the content-script match
   pattern and the host permission the options page asks for.

2. Add the host to `HARVEST_SOURCES` in `app/services/harvest.py`:

   ```python
   "example.com": "example_harvest",
   ```

   Skipping this does not break harvesting — the extractor never looks at the
   host — but the site's yield is filed under LinkedIn's source name, where you
   cannot tell the two apart.

3. Reload the extension, open its options page, tick the new box (it will ask
   for that host's permission), and browse a few job pages on that site.

4. Check the **Harvest by site** table on `/runs`.

That is the whole procedure. There is no parser to write.

## When a site stops working

The one failure mode this design still has: a site renames *every* field at
once. The reader keeps running, keeps forwarding, and finds nothing.

A zero on its own is not the signal — browsing a feed forwards plenty of
responses that legitimately contain no jobs, so "found 0" happens many times a
day. The signal is the change: a site that was yielding, still has traffic, and
now yields nothing.

`agent_events.harvest_health` finds that by comparing each site's **last 25
forwarded payloads against the 25 before them**. Counted, not dated — and that
matters:

- **It works the day you turn it on.** A calendar comparison ("this week versus
  last week") can say nothing until a week of history exists. Thirty job pages
  in one afternoon is already a complete before-and-after.
- **A site you browse rarely is not called broken for being quiet.** Each site
  is measured against its own traffic, so opening Wellfound twice a month is
  judged on those visits rather than against LinkedIn's volume.
- **A gap in your browsing is not a regression.** If you did not open Glassdoor
  for a fortnight, its last known state is still its current state, and the
  panel says *Working* rather than raising an alarm about your holiday.

A site needs 15 recent payloads before anything is claimed at all; below that
the panel says so instead of guessing.

The verdicts:

| Verdict | Means |
| --- | --- |
| **Working** | Jobs came out of its recent responses. Nothing to do. |
| **Stopped finding jobs** | Its last 25 responses carried nothing job-shaped, and the 25 before them did. The site probably renamed its fields. |
| **Forwarding, never finds jobs** | Responses are arriving but nothing job-shaped has ever come out. Either its payloads use names the reader does not know, or you only opened listing pages. |
| **Not browsed enough to say** | Fewer than 15 recent responses. Usually means you have barely opened the site. |

### Fixing a site that regressed

1. Open a job page on that site with DevTools on the **Network** tab, filtered
   to Fetch/XHR.
2. Find the response carrying the job data — it will be the large JSON one that
   appears when the posting renders.
3. Compare its field names against the alias tuples in
   `app/services/harvest.py`. You are looking for the keys holding the title,
   the company, the location, the description, the URL, and the id.
4. Add whichever names are new to the matching tuple. Order matters only in
   that the first match wins, so put more specific names first.
5. Add a fixture to `tests/test_harvest.py` with a trimmed copy of the payload.

Only the field names change; the walk does not. In practice this is a handful
of strings rather than a parser.

### If you want to send me the payload

Save the response body from step 2 to a file and strip anything personal from
it — the job objects are all that matters. A single job's worth of JSON is
enough to fix the aliases.

## What is not sent

`interceptor.js` forwards a response only when all of the following hold:

- The request URL matches `/(job|posting|search|hiring)/i`.
- The body is under 3 MB.
- The body contains one of `"title"`, `"jobTitle"`, `"companyName"`,
  `"jobPostingId"`.

Anything else is dropped in the page, before it reaches the extension. Only
sites you have ticked are instrumented at all — the content scripts are
registered at runtime from the toggles, not declared in the manifest, so an
unticked site has no script running on it.
