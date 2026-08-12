# JobApp Agent — browser extension

The hands. The server decides what work exists; this runs the part that needs
*your* browser — your residential IP, your logged-in sessions, your real
fingerprint — and posts the results back.

It runs two task kinds today:

| Kind | What it does | Needs |
|---|---|---|
| `ping` | Echoes its payload back. Proves the round trip without depending on any site being up. | nothing |
| `resolve_link` | Follows an aggregator redirect to the employer's real apply page. | **Resolve job links** ticked |

Job harvesting and autofill are items 8–9 and slot into `HANDLERS` in
`background.js` without changing anything else.

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

It is off by default and asks separately because it needs permission to read any
site — a materially larger ask than reaching your own server, and one that
should not be bundled into setup. Untick it and the permission is revoked; the
extension then stops claiming `resolve_link` work, leaving it queued for an
engine that can run it.

Those requests are sent **without cookies**. Resolving a public redirect does
not need your sessions, and sending them to an arbitrary aggregator would widen
what this can leak for no benefit.

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
| `POST /api/agent/tasks/<id>/fail` | Failure. `{error, agent_id}` — server decides retry or retire |
| `POST /api/agent/tasks/<id>/heartbeat` | Extend the lease on long-running work |

A lease is exclusive and time-limited. If this browser closes mid-task the lease
lapses and the task returns to the queue for whoever asks next — no attempt is
counted against it, since a closed laptop is not a failed attempt.

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

Nothing is read from pages you browse. `resolve_link` fetches only URLs the
server explicitly queued, all of them aggregator links that came from your own
job results.
