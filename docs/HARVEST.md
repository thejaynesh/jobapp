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

## Having the extension open the pages for you

Harvesting was never limited by what it could read — it reads a site's own API
responses. It was limited by *attendance*: nothing is harvested from a page
nobody opened.

The **Browse for me** controls on `/runs` queue the opening. The extension
opens each URL in a hidden window, lets it finish loading, scrolls a few
screens, and closes it. The interceptor runs on that tab exactly as it does on
one you opened yourself.

- **Fill in descriptions** opens stored jobs that have a thin description and a
  URL on a browsable board. This is where most of the value is: a harvested
  search card has a title and an id and usually no body.
- **Crawl boards** starts from each board — a search per target role for sites
  whose search is a URL, and the board's own feed for sites whose search is
  rendered from an internal API.
- **Paste your own URLs** takes anything you copied out of the address bar.

### How a board's depth is reached

A board's second page comes from one of three places, and picking the wrong one
harvests page one forever while every number on the panel looks healthy. That is
the worst shape of bug this feature has: the visit succeeds, the scroll reports
a sensible depth, rows arrive — and they are the same rows every time.

| Board | Depth comes from | Set with |
| --- | --- | --- |
| LinkedIn, Amazon, Google | A **URL** — `start=25`, `page=2` | `page_param` / `page_size` / `page_base` |
| Greenhouse's board, JobRight | A **scroll**. No page two exists; the scroll *is* the pagination | `scroll_passes` |
| Hiring Cafe | A **click** on a numbered control, with one address for every page | `click_pages` |

The third had no mechanism at all until it was reported. Hiring Cafe was
configured as an entry page with the default scroll, which reaches the bottom of
page one, finds nothing more, and closes.

**Clicking, not navigating.** These are single-page apps, so page two is often
the same URL — there is no address to queue. The extension finds the next-page
control and clicks it, exactly as a person would, and the interceptor reads the
request that click causes.

Finding the control is a guess at somebody's markup, so it is deliberately
narrow: `rel="next"` first, then a button or link whose *whole label* is
"Next", "›", "»" or similar. An element whose text merely contains "next page"
is usually a container holding one, so the label has to be short to count.

After clicking, it waits for the list to actually turn over — the first forty
`href`s, compared before and after. Node counts cannot see this: twenty rows
replaced by twenty rows is the same count. If nothing changes, it stops rather
than clicking again, because re-harvesting one page repeatedly looks like depth
and is worse than stopping.

**When it does not find the control**, the visit still scrolls, still harvests,
and still looks fine — so the number of pages reached is reported and a board
asked for ten pages that reached one logs:

> hiring.cafe was asked for 10 pages and reached 1 — the next-page control was
> not found, so only the first page was harvested

That is the line to search for if Hiring Cafe stops yielding new jobs. The fix
is a selector, not a parser.

### Why some boards get a feed instead of a search

`BOARDS` in `app/services/browse_plan.py` gives each site either a `search`
template or a list of `entries`.

LinkedIn gets a template: its search URL is public, stable, and unchanged for a
decade. JobRight, Hiring Cafe and Handshake get entry pages, because their
results are rendered from internal APIs whose query parameters are private and
subject to change without notice. Guessing at those produces a crawl that opens
error pages very politely, sixty times, through your logged-in session.

For those boards, run the search yourself, copy the URL, and paste it in. That
URL is correct by construction, which no guess can be.

### Boards that send cards, not postings

Some boards' search responses carry no description at all — not a short one, not
a snippet. The card is a card. Greenhouse's aggregate board
(`my.greenhouse.io`) is the clearest case: every row has a title, a company, a
location, a pay band and a URL, and no body text anywhere in the payload.

**This is not a scroll problem and more browsing does not fix it.** Scrolling
further gets more cards.

What fixes it is the second step, which runs on the server: the card names the
employer's own board URL, and if that URL belongs to an ATS we can read
(`job-boards.greenhouse.io/<slug>/jobs/<id>` does), enrichment fetches the full
description from that ATS's public API. One free request, no browser, no tab.

So a harvested Greenhouse job arrives empty and fills in on the next enrichment
pass. That is the design, not a fault — the browser is there for the pages a
server cannot reach, and Greenhouse's own API is not one of them.

Two details make it work, both in `_normalize`:

- `publicUrl` counts as a job URL. It is the *only* URL on these rows, and
  `_normalize` drops any job it cannot address — so before it was an alias, the
  entire board harvested as zero jobs, which is indistinguishable from an idle
  browser.
- `viewJobPath` (`/jobs/<slug>/<id>`) is turned into a canonical Greenhouse URL
  and stored as the apply URL. This matters when `publicUrl` points at the
  employer's own careers page instead — `ifit.com/careers?gh_jid=123` names the
  job but not the company. Deriving the slug buys two things: a description by
  API rather than a scrape, and the slug itself, which is that company's entire
  board on every future fetch cycle.

That last point is the one worth remembering. A harvested posting is one job,
once. A slug found on this board is a permanent new source.

### Pacing

This drives a real browser through a logged-in session, and volume plus rhythm
is what anti-automation systems measure. The cost of getting it wrong is the
account, not the run — so the defaults are deliberately slow:

| Setting | Default | What it does |
| --- | --- | --- |
| `BROWSE_MAX_QUEUED` | 60 | Pages per triggered run |
| `BROWSE_GAP_SECONDS` | 20 | Minimum wait between one page closing and the next opening |
| `BROWSE_SETTLE_SECONDS` | 6 | How long a page stays open after `load` |
| `BROWSE_RETRY_DAYS` | 30 | Don't reopen a page browsed this recently |

The settle matters more than it looks: LinkedIn fetches the posting body
*after* `load` fires, so a tab closed promptly harvests nothing at all.

A run of 60 takes roughly half an hour. That is the feature. If you raise these
numbers, raise them a little.

The extension must have **"Open blocked pages in a hidden window"** ticked —
that toggle is what governs opening windows at all, so browsing is not claimed
without it.

## When a board asks you to slow down

An infinite list rate-limits. Greenhouse's board stops sending cards after
enough scrolling and asks you to wait a few minutes.

The scroll loop used to keep going — it saw no new content, counted it as a
stall, and scrolled again, spending the rest of its 75-second budget on
requests into a closed door. That is how "a few minutes" becomes longer, and
the visit came back reporting a shallow crawl, which reads as a broken scroll
target rather than a board that said no.

A rate limit is a *rate*, and three things follow from that:

**Stop on sight.** The scroll now watches for the wording and breaks out the
moment it appears. Further passes buy nothing.

**Rest for minutes, not hours.** `BROWSE_RATELIMIT_REST_MINUTES` (20 by
default) — the board said "not this fast", not "not at all". Nothing is lost by
waiting: the cards harvested before the limit were posted to `/harvest` as they
arrived and are already stored.

**Go slower, not just shorter.** This is the part that actually buys depth. The
next visit to a board that objected gets `BROWSE_SCROLL_PAUSE_SECONDS` between
batches, and a pass count set from how far it got last time — a little under,
because the depth a limit bites at moves around and sitting on the last known
edge finds it again about half the time. A smaller pass count on its own only
decides where you give up; a pause is what lets you get further.

Boards that have never objected are untouched by all of this: no pause, no cap.
Slowing everything down to accommodate one board would be depth given away for
nothing.

### The one backoff a button does not override

Everywhere else, pressing a button means *do it now* and skips the cooloff. Not
here. A human check being watched by a human is answerable — that is what it is
asking for. A board that said "wait a few minutes" says it just as firmly to
somebody sitting at the keyboard, so queueing anyway spends a request to be told
again. `/runs` answers with *when* instead:

> my.greenhouse.io asked us to slow down. Resting it for up to 20 minutes — try
> again after that.

### There is no resuming part-way

An infinitely scrolling board has no page-two URL, so a visit cut short at pass
40 cannot be picked up at 41 — the next one re-walks the top of the list. That
is why staying under the limit matters more than recovering from it, and why
the depth cap is worth more than it looks: the same ground gets covered without
the penalty.

It also matters less than it sounds. A board filtered to the last day (as the
Greenhouse feed URL is) does not have infinite depth worth reaching — the
scroll only has to cover what was posted since the last visit.

## When a site asks you to confirm you're human

Some sites put a check in front of the page. Jooble does it on its `away`
redirects, which is exactly the URL link resolution has to open to find the
employer's real apply link.

The window used to open minimized and close a few seconds later, so the check
appeared and vanished with nobody able to click it. Every visit failed, and on
the panel that looked identical to a page with nothing on it — which points the
blame at the reader, and the reader was fine.

**Now the extension shows you the window and waits.** When it detects a check
it raises the window, gives you 90 seconds, and carries on from where it left
off once you've clicked. Passing one usually sets a cookie good for a while, so
the click buys more than the single page it was spent on.

Nothing is clicked for you. Satisfying the check is the point of the check;
the only bug was never giving you the chance.

Turn it off with **"Show me 'confirm you're human' checks"** in the extension's
options if you'd rather those visits just fail quietly.

### If nobody answers

The check is only offered once per site per session. Without that, sixty queued
Jooble visits would be sixty windows raised at an empty chair, each holding the
browser for 90 seconds while every working board waits behind it.

The server backs off too. A host that asked and got no answer is left alone for
`BROWSE_CHALLENGE_BACKOFF_HOURS` (24 by default) — new pages are not queued for
it from either the crawl or the enrichment backlog, and `/runs` names it:

> Asked for a human check: jooble.org. Left alone for now.

The backoff is deliberately short and re-earned. These checks are usually about
the traffic pattern rather than the visitor, so a host that blocked us this
morning is worth one attempt tomorrow — and passing the check is never held
against a host, or the click would stop the pages it just unlocked.

A crawl **you press a button for** ignores the backoff entirely. Its whole
premise is that nobody is at the keyboard, and pressing a button disproves
that.

## When a site says it has noticed you

You may get a mail or an interstitial like this one, which is real and worth
taking at face value:

> We noticed some unusual activity on your account. Over time, our systems have
> shown your account has accessed a high volume of LinkedIn profile data. This
> often happens because of a third-party tool or browser extension...

That is an accurate description of driven browsing, and the cost of ignoring it
is the account rather than the run.

**Pause that host and leave the rest running.** Set `BROWSE_PAUSED_HOSTS` and
restart:

```
BROWSE_PAUSED_HOSTS=linkedin.com
```

Subdomains are covered, so `linkedin.com` is enough for `uk.linkedin.com`.
Several hosts are comma-separated. This stops new pages being queued, drops the
ones already waiting, and shows the board struck through on `/runs` so the
pause is visible rather than looking like a crawler that broke.

If you need to stop it in the next thirty seconds and have no shell, untick
**"Open blocked pages in a hidden window"** in the extension's options. That
stops every site rather than one, which is the right trade in a hurry.

### Harvesting is not the thing that got flagged

Worth separating, because the safe half of this feature is easy to throw away
along with the risky half:

| | What it does | Risk |
| --- | --- | --- |
| **Harvesting** | Reads job data out of the responses pages already received, on tabs you opened yourself | Makes no extra requests. There is no traffic to notice. |
| **Driven browsing** | Opens pages on its own — up to `BROWSE_MAX_QUEUED` a run, on a schedule, through your logged-in session | This is the volume a site measures. |

So a paused host is still harvested when you browse it yourself. Only the
opening stops. `BROWSE_PAUSED_HOSTS` does not touch `HARVEST_SOURCES` and the
per-site checkboxes do not touch each other.

### Turning a site's own checkbox off

Unticking a site in the extension's options does both halves for that site: it
revokes the host permission and unregisters the reader, *and* the extension now
refuses to open pages there even if the server queues some.

That second half was missing until this warning arrived. Unticking a site
stopped the reading and left the opening running — the same traffic through the
same session with nothing to show for it, which is the worst of both and was
worst in exactly the case the checkbox exists for.

### If you turn it back on

Come back gently. `BROWSE_SEARCH_PAGES` and `BROWSE_MAX_QUEUED` multiply — five
pages across six roles and two locations is sixty visits, which is a full run
every time the schedule fires. Halving both, or dropping the host from the
scheduled sweep and only crawling it by hand, is a smaller footprint than the
defaults.

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
