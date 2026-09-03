# JobApp Agent — browser extension

The hands. The server decides what work exists; this runs the part that needs
*your* browser — your residential IP, your logged-in sessions, your real
fingerprint — and posts the results back.

It runs two task kinds today:

| Kind | What it does | Needs |
|---|---|---|
| `ping` | Echoes its payload back. Proves the round trip without depending on any site being up. | nothing |
| `resolve_link` | Follows an aggregator redirect to the employer's real apply page. | **Resolve job links** ticked |
| `fetch_json` | Fetches a public JSON endpoint the server is blocked from — Reddit refuses datacenter IPs outright. | **Resolve job links** ticked |

It also harvests passively and draws an on-page overlay — see below. Autofill is
item 9 and slots into `HANDLERS` in `background.js` without changing anything
else.

## Installing

Chrome, Edge, Brave, or any other Chromium browser:

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. **Load unpacked**, and pick this `extension/` directory
4. Open the extension's options (its toolbar icon, or **Details → Extension options**)

Then fill in:

| Field | Value |
|---|---|
| Server URL | Where you reach the app, e.g. `https://your-domain.example` |
| Agent token | The `AGENT_TOKEN` from your server's `.env` — **not** your login password |

Click **Test connection**. A green line reporting queue depth means the token
works and the server is reachable. Tick **Poll for work** and save.

### Resolve job links

A second, separate toggle. Aggregators (Adzuna, Jooble, Indeed) link to their
own redirect page rather than the employer, and following those from your VPS is
exactly the request a datacenter IP gets blocked on. Your browser is not
blocked, so the server hands over what it could not follow and the real apply
link comes back.

This toggle also enables `fetch_json`, which fetches public JSON the server is
walled out of. Reddit answers a datacenter IP with `403 Blocked` — a categorical
refusal rather than a rate limit, so no amount of retrying from the VPS works.
Interview-report gathering depends on it.

**Log in to Reddit in this browser.** Reddit refuses anonymous JSON whoever is
asking, so a residential address alone is not enough; the session is what makes
the request answerable. Requests to reddit.com therefore carry your cookies —
and only reddit.com does. That list lives in the extension, not in the task, so
the server cannot queue a URL that causes your cookies to be sent anywhere new.
Every other queued fetch is anonymous.

It is off by default and asks separately because it needs permission to read any
site — a materially larger ask than reaching your own server, and one that
should not be bundled into setup. Untick it and the permission is revoked; the
extension then stops claiming `resolve_link` work, leaving it queued for an
engine that can run it.

Those requests are sent **without cookies**. Resolving a public redirect does
not need your sessions, and sending them to an arbitrary aggregator would widen
what this can leak for no benefit.

### Harvest jobs from the sites you browse

One toggle per site, and the only feature that reads pages you visit.

While you browse normally, the page asks its own API for job cards and receives
far more than it renders — full descriptions, applicant counts, salary bands.
This reads those responses as they arrive and forwards them to your server.
**No extra requests are made.** Nothing is fetched, nothing is clicked, nothing
is automated; the traffic is a person using the site, because it is.

It also needs no API keys, doc IDs or query IDs. The wrapper reads a copy of
whatever the page already fetched, so whichever identifiers the site rotates
are the site's problem rather than something to keep up to date here.

That matters most for LinkedIn specifically. The guest API your server polls
returns ten cards a page and needs a separate request per description, which is
what makes `LINKEDIN_MAX_DETAIL_FETCHES` the real ceiling on that source.
Voyager returns descriptions inline, so the ceiling disappears — and harvested
copies merge into jobs you already have, filling in descriptions the guest API
never returned.

Each site is a **separate permission**, asked for when you tick its box and
given back when you untick it — "read every job board you visit" is a different
thing to agree to than "read LinkedIn", so they are not bundled. The content
scripts are registered from the toggles at runtime rather than declared in the
manifest, so installing the extension requests nothing on behalf of a feature
that is off, and an unticked site has no script running on it at all.

The list of sites lives in one place, `sites.js`, and both this worker and the
options page read it. Adding a site is a row there plus a source name on the
server — see [docs/HARVEST.md](../docs/HARVEST.md).

### Open blocked pages in a hidden window

Some sites refuse a background request outright — Jooble and Indeed both answer
`403` to a `fetch` from the extension while opening the same URL in a tab works
fine. They are not blocking your browser; they are screening for the *shape* of
the request. A `fetch` from a service worker sends no Referer, runs no
JavaScript, paints nothing and follows no meta-refresh.

So when a fetch is refused, the extension reopens the URL as a real page load in
a **minimized window**, reads where it landed and what it rendered, and closes
it. A real navigation is not an imitation of a browser visit; it is one.

Deliberately an escalation rather than the default: a fetch is silent and cheap,
a window is not. It runs **one at a time**, so a backlog of link resolutions
cannot open a dozen windows at once, and each closes as soon as it has been
read. `/runs` marks any result obtained this way as *opened as a page*, so you
can see how often it is needed.

Only refusals escalate — 401, 403, 405, 406, 429, 503 and network errors. A 404
means the page genuinely is not there, and reopening it would cost a flicker to
learn the same thing.

Untick the toggle and those tasks simply fail instead.

#### Why two content scripts

`interceptor.js` runs in the **MAIN** world, sharing the page's globals, which
is the only way to patch the `fetch` the page itself calls — a normal content
script gets an isolated copy of `window` and would patch a `fetch` nobody uses.
That same sharing means it has no `chrome.*` to reach the extension with. So
`relay.js` runs in the isolated world, receives findings over `postMessage`, and
forwards them. Neither half can do the other's job.

`postMessage` is a public channel, so the relay verifies the message came from
this same window and treats the contents as untrusted. The server decides what
is job-shaped and stores nothing it cannot recognize.

#### Why it reads JSON rather than the page

Scraping the DOM is the wrong layer. CSS classes are regenerated on every
redesign, so selector-based extraction rots on someone else's schedule — and it
rots silently, since a changed class name yields zero jobs, which looks exactly
like an empty page.

The parser on the server is shape-based for the same reason: it walks the whole
payload looking for anything with a title, a company, and an identifier, rather
than following `elements[].jobCardUnion.jobPosting.title`. LinkedIn can
reorganize its response and the harvest keeps working — and a site nobody wrote
a parser for usually works on the first try, which is why adding one is a row
in `sites.js`.

The failure this design still has is a payload renaming *every* field at once.
Then the reader keeps running, keeps forwarding and finds nothing, which is
invisible. So the **Harvest by site** table on `/runs` splits its window in half
and compares: a site that was yielding, still has traffic, and now finds nothing
is reported as *Stopped finding jobs*. Fixing one is a handful of field aliases
— see [docs/HARVEST.md](../docs/HARVEST.md).

### Show the overlay on job pages

A small panel on the job posting itself, answering the three things worth
knowing before you spend an hour on an application: **have I seen this, what did
it score, did I already apply.** All three were already in your tracker; the
only reason they were hard to reach is that they lived on a different tab.

It also carries what the eligibility scan found — the amber *no sponsorship* and
red *US citizens only* flags — and a button that saves the posting and starts
writing documents for it. That button works on postings your pipeline never
fetched: it reads the page's own JSON-LD, which is what ATS boards publish for
Google Jobs, so an employer's careers page you found yourself is one click from
an application.

It asks for the named boards only — LinkedIn jobs, Greenhouse, Lever, Ashby,
Workday, Workable, SmartRecruiters, Recruitee — rather than a wildcard.

#### Filling a form

On a page with several empty fields, the panel offers **Fill this form**. It
matches each field against your profile using everything the field is described
by at once — `autocomplete`, `name`, `id`, `placeholder`, `aria-label`, and the
visible label — because every ATS names them differently and no single attribute
is reliable.

Three rules it will not break:

- **It never submits.** It fills and stops. You read it and press apply.
- **It never overwrites an answer.** A field with anything in it is skipped, so
  a half-completed form cannot be clobbered.
- **Everything it wrote is outlined in blue.** You are about to send this to an
  employer, so what a machine typed has to be obvious at a glance.

Values are fetched when you press the button, not on page load, so your details
reach a page only when you have asked for them to be typed there. What travels
is a fixed list — name, email, phone, location, links, your most recent degree,
and the five screening answers below. Your narrative, preferences, templates,
match scores and application history are not part of it.

#### The five questions, answered once

**Profile → Screening.** Work authorization, whether you need sponsorship,
earliest start date, salary expectation, and how you heard about us. Every ATS
asks all five, in a different order and with different wording, and they are the
part of an application that actually takes the time.

They are free text rather than a fixed yes/no, because the same fact is a
sentence in one form's textarea and an option in another's dropdown — and the
autofill now handles `<select>` as well as text inputs, matching your written
answer against the options.

Two refusals worth knowing about, both for the same reason — these are
declarations going to an employer, and a wrong one looks deliberate in a way an
empty box does not:

- **A dropdown whose options don't clearly contain your answer is left alone**
  and named in the panel, rather than set to the nearest option.
- **A question that asks about sponsorship and authorization at once** — "are
  you authorized to work without requiring sponsorship?" is real and common —
  **is skipped entirely.** The two are asked inverted from each other, and there
  is no way to tell from the field which way round this one means it.

#### Attaching your resume

**Attach resume** puts the tailored PDF into the form's file input. This is the
one part of an application autofill could never reach, and it works because a
content script genuinely can set `input.files` through a `DataTransfer`. What it
cannot do is fetch the file — that is behind your token — so the service worker
fetches it and passes the bytes through.

If the page has several uploads and none of them says which is the resume, it
says so and leaves them alone: a cover letter filed as a resume is worse than an
empty slot you fill yourself.

#### Marking it applied

**Mark applied** records the application from the page you applied on. The
moment you press Submit on the employer's form is the only moment you know for
certain that you applied, and it is the moment you are furthest from the
tracker — every application marked days later, or never, is that gap. Pressing
it twice is safe, and it never walks an interviewing application backwards.

Two things about how it is built. It draws inside a **closed shadow root**,
because job sites ship aggressive global CSS and a plain injected div inherits
all of it; a panel that looks right on Greenhouse would be unreadable on
Workday. And it **never sees your token** — the panel asks the service worker,
which makes the request, so the credential never enters the page's process.
Nothing is requested until you open the panel.

The browser will ask permission to access your server's address. It is requested
at save time rather than declared in the manifest because the address is
different for every deployment — declaring it up front would mean an install
prompt asking for access to every website, in order to talk to one.

### If the server is on a different origin

Set `CORS_ALLOW_ORIGINS` in the server's `.env` to the extension's origin, which
`chrome://extensions` shows as its ID:

```
CORS_ALLOW_ORIGINS=chrome-extension://abcdefghijklmnopabcdefghijklmnop
```

The ID is stable for an unpacked extension as long as the directory does not
move.

## How the loop works

```
alarm (1 min)  →  POST /api/agent/lease   (waits up to 25s for work)
                    ↓ tasks
                  run each handler
                    ↓
                  POST /api/agent/tasks/<id>/result   (or /fail)
                    ↓
                  poll again immediately if anything was done
```

Two Chrome facts explain the odd-looking numbers.

**The service worker is killed after ~30 seconds idle.** It is not a daemon. So
the long poll caps at 25 seconds — an in-flight fetch keeps the worker alive,
but a poll longer than the idle timeout would be waiting in a worker that no
longer exists. Nothing is kept in memory between wakeups; state lives in
`chrome.storage`.

**`chrome.alarms` cannot fire more often than once a minute.** So freshly queued
work waits up to a minute for someone to ask about it. Draining is much faster:
after finishing a batch the loop immediately polls again instead of waiting for
the next alarm, so ten queued tasks do not take ten minutes.

## The protocol

All endpoints need `Authorization: Bearer <AGENT_TOKEN>`. The server gates the
whole `/api/agent/*` prefix in middleware, so there is no unauthenticated
surface to find.

| Endpoint | Purpose |
|---|---|
| `GET /api/agent/hello` | Handshake: supported kinds, timings, queue depth |
| `POST /api/agent/lease` | Claim work. `{kinds, agent_id, max, wait}` → `{tasks, lease_seconds}` |
| `POST /api/agent/tasks/<id>/result` | Success. `{result, agent_id}` |
| `POST /api/agent/tasks/<id>/fail` | Failure. `{error, agent_id, permanent}` — `permanent` skips the retries |
| `POST /api/agent/tasks/<id>/heartbeat` | Extend the lease on long-running work |
| `POST /api/agent/harvest` | Offer intercepted job JSON. `{payload, source_url}` — a push, not a task |
| `POST /api/agent/link` | Hand over a board's own credential. `{site, api_key, refresh_token}` — see below |
| `GET /api/agent/job-context?url=` | What we know about a posting: score, flags, whether you applied |
| `GET /api/agent/autofill-fields` | The profile values a form asks for — a fixed list, not the profile |
| `POST /api/agent/prepare` | Save a posting and open an application for it. `{url, posting}` |

A lease is exclusive and time-limited. If this browser closes mid-task the lease
lapses and the task returns to the queue for whoever asks next — no attempt is
counted against it, since a closed laptop is not a failed attempt. (`lease`
counts one on every claim including a recovery, so `reap` gives that one back;
otherwise two lapsed leases would retire a task that had run once.)

A batch of up to five is leased at a single instant and then worked through one
task at a time, so the agent holds the leases open itself: while a batch is in
progress it heartbeats every task still outstanding, every `lease_seconds / 3`.
Without that, a deep board — a 75-second scroll budget plus loads, settles and a
pacing gap — outran its own 120-second lease, and the later tasks in the batch
were reaped and re-queued while this browser was still working on them.

Failures are retried up to three times, except when the agent marks them
`permanent`. It does that for a 4xx other than 408 and 429: a refused request
will be refused again, and three identical rows bury whatever else failed that
hour. A *timeout* is exactly the case worth retrying, which is why 408 is out.

Reporting a success is separate from doing the work. If the visit succeeded and
only the upload failed — the server restarted, the proxy answered 502, the wifi
dropped — nothing is reported at all: the lease lapses, the task comes back to
the queue, and no attempt is charged for a network we do not control. Posting a
`/fail` there used to burn an attempt for work that had been done correctly.

## Linking a board, so the server can sweep it without us

`tsenta.js` reads Tsenta's API from inside a tab, which works and which only
happens while a browser is open on that board. `link.js` closes that gap: it
reads the Firebase refresh token the site's own SDK stored, posts it to
`/api/agent/link`, and the server mints hour-long ID tokens from it against
Google's public `securetoken` endpoint — so the sweep runs on a schedule with
no browser at all.

Three things about it are deliberate:

**It is an isolated-world script, not a MAIN-world one.** Everything else the
extension reads off a page travels through `window.postMessage`, which every
script on the page can listen to — including the analytics and error-reporting
bundles a modern site loads from third parties. A job listing going past those
is nothing; a refresh token going past them is the account. An isolated content
script shares the origin's storage without sharing its globals, so it opens the
same IndexedDB the site's SDK wrote and calls `chrome.runtime.sendMessage`
directly.

**It searches rather than addresses.** The documented layout is a
`firebaseLocalStorageDb` database with a `firebase:authUser:<apiKey>:<appName>`
key, but the app name is the site's choice and the SDK has moved this before. It
walks the Firebase databases for anything holding a `stsTokenManager`, and falls
back to `localStorage` for a site using one of the other two persistences.

**It re-links on every visit.** That is the repair path: a credential that has
gone stale is fixed by opening the board, and nobody has to learn that is the
fix. The `/runs` panel says when a board was last linked and what the last mint
error was, so "the scheduled sweep stopped" reads as an instruction.

Adding a board means a `link:` entry in `sites.js`, a site name in
`LINKABLE_SITES` in `app/routers/agent.py`, and a sweep the scheduled task can
call. Everything else — minting, rotation, caching, the failure record — is in
`app/services/linked_auth.py` and is not per-site.

## Adding a task kind

1. Add the name to `TASK_KINDS` in `app/models/browser_task.py`
2. Add a handler to `HANDLERS` in `background.js`, keyed by that name
3. If the server should act on the result, add an entry to `RESULT_HANDLERS` in
   `app/services/agent_work.py`

`supportedKinds()` reports only what this install can currently run, so an agent
never claims work it would just fail. An older extension and a newer server
coexist safely: unknown kinds are simply never leased by that install, and stay
queued for one that understands them.

## Testing it end to end

On the server:

```python
from app.database import SessionLocal
from app.services import browser_tasks
db = SessionLocal()
task = browser_tasks.enqueue(db, "ping", {"hello": "world"})
print(task.id)
```

Within a minute the extension leases it, runs it, and posts back. Check:

```python
db.expire_all()
print(db.get(type(task), task.id).result)
# {'pong': True, 'echo': {'hello': 'world'}, 'at': '...'}
```

## Privacy

The extension reports to exactly one server: the one you configure. No
analytics, no third-party requests, no remote code.

Two permissions, asked for separately because they are not the same ask:

- **Your server's origin**, requested when you save settings. Needed to reach
  the API at all.
- **Any site**, requested only when you tick **Resolve job links**, and removed
  when you untick it. Used to follow aggregator redirects, with cookies
  omitted, and for nothing else — the extension never leases a task kind it
  cannot run, so declining this leaves that work queued rather than attempted.
- **One harvest site**, requested per box when you tick it and removed when you
  untick it. Ticking LinkedIn buys access to linkedin.com and nothing else;
  every other site is its own separate ask.
- **The named job boards**, requested only when you tick **Show the overlay on
  job pages**. The overlay reads the page it is on only when you open the
  panel, and only to fill in a posting you asked it to save.

With every harvest box off, nothing is read from pages you browse at all:
`resolve_link` fetches only URLs the server queued, every one of them an
aggregator link that came from your own job results.

With a box on, job data from that site's pages is sent to your own server and
nowhere else. Only responses whose URL looks job-related and whose body
contains job-shaped JSON are forwarded — the filter runs in the page, before
anything reaches the extension — and the server discards whatever it cannot
recognize as a posting.
Messages, connections and your feed are not job-shaped and do not survive the
parser — but the honest statement is that the interceptor sees LinkedIn API
responses on pages you open, and forwards the ones that mention a job title or
company. Untick it and the scripts are unregistered outright.
