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

### Harvest jobs from LinkedIn

A third toggle, and the only feature that reads pages you visit.

While you browse LinkedIn normally, the page asks its own API for job cards and
receives far more than it renders — full descriptions, applicant counts, salary
bands. This reads those responses as they arrive and forwards them to your
server. **No extra requests are made.** Nothing is fetched, nothing is clicked,
nothing is automated; the traffic is a person using the site, because it is.

That matters most for LinkedIn specifically. The guest API your server polls
returns ten cards a page and needs a separate request per description, which is
what makes `LINKEDIN_MAX_DETAIL_FETCHES` the real ceiling on that source.
Voyager returns descriptions inline, so the ceiling disappears — and harvested
copies merge into jobs you already have, filling in descriptions the guest API
never returned.

It asks for **linkedin.com only** — not the broad access that link resolving
needs. The content scripts are registered when you tick it and unregistered when
you untick it, rather than declared in the manifest, so installing the extension
does not request LinkedIn access for a feature that is off.

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
reorganize its response and the harvest keeps working.

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
is a fixed list — name, email, phone, location, links, and your most recent
degree. Your narrative, preferences, templates, match scores and application
history are not part of it.

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
| `POST /api/agent/lease` | Claim work. `{kinds, agent_id, max, wait}` |
| `POST /api/agent/tasks/<id>/result` | Success. `{result, agent_id}` |
| `POST /api/agent/tasks/<id>/fail` | Failure. `{error, agent_id, permanent}` — `permanent` skips the retries |
| `POST /api/agent/tasks/<id>/heartbeat` | Extend the lease on long-running work |
| `POST /api/agent/harvest` | Offer intercepted job JSON. `{payload, source_url}` — a push, not a task |
| `GET /api/agent/job-context?url=` | What we know about a posting: score, flags, whether you applied |
| `GET /api/agent/autofill-fields` | The profile values a form asks for — a fixed list, not the profile |
| `POST /api/agent/prepare` | Save a posting and open an application for it. `{url, posting}` |

A lease is exclusive and time-limited. If this browser closes mid-task the lease
lapses and the task returns to the queue for whoever asks next — no attempt is
counted against it, since a closed laptop is not a failed attempt.

Failures are retried up to three times, except when the agent marks them
`permanent`. It does that for a 4xx other than 429: a refused request will be
refused again, and three identical rows bury whatever else failed that hour.

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
- **linkedin.com**, requested only when you tick **Harvest jobs from
  LinkedIn**, and removed when you untick it.
- **The named job boards**, requested only when you tick **Show the overlay on
  job pages**. The overlay reads the page it is on only when you open the
  panel, and only to fill in a posting you asked it to save.

With harvest off, nothing is read from pages you browse at all: `resolve_link`
fetches only URLs the server queued, every one of them an aggregator link that
came from your own job results.

With harvest on, job data from LinkedIn pages you visit is sent to your own
server and nowhere else. Only responses containing job-shaped JSON are
forwarded; the server discards anything it cannot recognize as a posting.
Messages, connections and your feed are not job-shaped and do not survive the
parser — but the honest statement is that the interceptor sees LinkedIn API
responses on pages you open, and forwards the ones that mention a job title or
company. Untick it and the scripts are unregistered outright.
