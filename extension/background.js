/**
 * The agent loop: ask the server for work, run it, post the result back.
 *
 * The server never calls us. It cannot — this browser has no address, and it is
 * closed half the day. So the flow is entirely pull-based, and being offline is
 * not an error state, it is just a period where no work gets leased.
 *
 * Two facts about MV3 service workers shape everything here:
 *
 *   1. This worker is terminated after roughly 30 seconds idle. It is not a
 *      daemon. Anything remembered between wakeups lives in chrome.storage, and
 *      the poll ceiling stays under that timeout so a request never outlives
 *      the worker that is waiting on it. An in-flight fetch counts as activity,
 *      which is why a 25-second long poll survives and a 45-second one dies.
 *
 *   2. chrome.alarms is the only way to be woken reliably, and its floor is one
 *      minute. So the worst case for an idle queue is: work is enqueued, and up
 *      to a minute passes before anyone asks for it. Draining is much faster
 *      than that — after a task completes we poll again immediately rather than
 *      waiting for the next alarm, so a burst of ten tasks does not take ten
 *      minutes.
 */

// The sites harvesting can read. Shared with the options page rather than
// written out in both — see sites.js for how to add one.
import { HARVEST_SITES } from "./sites.js";

const ALARM_NAME = "jobapp-poll";
const DEFAULTS = {
  serverUrl: "",
  token: "",
  agentId: "",
  enabled: false,
};

// ---------------------------------------------------------------------------
// Configuration and status
// ---------------------------------------------------------------------------

async function getConfig() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

/**
 * Status is written to storage rather than kept in memory, because this worker
 * is disposable — the options page needs to be able to show what happened on a
 * poll that ran in a worker which no longer exists.
 */
async function setStatus(patch) {
  const current = (await chrome.storage.local.get("status")).status || {};
  await chrome.storage.local.set({
    status: { ...current, ...patch, updatedAt: new Date().toISOString() },
  });
}

/** A stable-enough name for this install, so the server can tell engines apart. */
async function agentId() {
  const config = await getConfig();
  if (config.agentId) return config.agentId;
  const generated = `extension-${crypto.randomUUID().slice(0, 8)}`;
  await chrome.storage.local.set({ agentId: generated });
  return generated;
}

// ---------------------------------------------------------------------------
// Talking to the server
// ---------------------------------------------------------------------------

async function api(path, body, { timeoutMs = 40000 } = {}) {
  const { serverUrl, token } = await getConfig();
  if (!serverUrl || !token) throw new Error("Not configured yet.");

  // An abort guard, because a hung request would otherwise keep this worker
  // alive doing nothing until the browser loses patience with it.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(new URL(path, serverUrl).toString(), {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });

    // Read the server's own explanation where there is one. These messages are
    // written to be shown to a person — "AGENT_TOKEN is not set" is a fixable
    // instruction in a way that "HTTP 503" is not.
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (_) {
        /* not JSON; the status line is all we have */
      }
      throw new Error(detail);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Opening a page for real
// ---------------------------------------------------------------------------

/**
 * Load a URL in an actual browser window, when fetching it is refused.
 *
 * A `fetch` from a service worker sends no Referer, runs no JavaScript, paints
 * nothing and follows no meta-refresh. Aggregators screen for exactly that
 * shape, which is why Jooble and Indeed answer 403 here while the same URL
 * opens fine in a tab. A real navigation is not an imitation of a browser
 * visit; it is one.
 *
 * The window is created minimized and closed again immediately. That is still
 * more intrusive than a fetch, so this is an escalation rather than the default
 * path — cheap and silent first, visible only when that fails.
 */
const TAB_LOAD_TIMEOUT_MS = 25000;
// Time after "complete" for a meta-refresh or JS redirect to happen. The
// interstitials this exists for very often bounce a moment after painting.
const TAB_SETTLE_MS = 1800;

// One at a time. Escalating a backlog of link resolutions in parallel would
// open a dozen windows at once, which is not a thing to do to someone's screen.
let tabQueue = Promise.resolve();

function withTabLock(fn) {
  const run = tabQueue.then(fn, fn);
  // Keep the chain alive whichever way this settles.
  tabQueue = run.then(() => {}, () => {});
  return run;
}

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") finish();
    };
    // A page that never finishes must not hold the queue open; whatever has
    // loaded by then is usually enough to read a redirect target out of.
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function openInTab(url) {
  return withTabLock(async () => {
    let win;
    try {
      win = await chrome.windows.create({
        url,
        focused: false,
        state: "minimized",
      });
    } catch (error) {
      throw new Error(`could not open a window: ${error.message}`);
    }

    const tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
    if (!tabId) {
      await chrome.windows.remove(win.id).catch(() => {});
      throw new Error("the window opened without a tab.");
    }

    try {
      await waitForLoad(tabId, TAB_LOAD_TIMEOUT_MS);
      await new Promise((r) => setTimeout(r, TAB_SETTLE_MS));

      const tab = await chrome.tabs.get(tabId);
      let html = "";
      let text = "";
      try {
        const [injected] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => ({
            html: document.documentElement ? document.documentElement.outerHTML : "",
            text: document.body ? document.body.innerText : "",
          }),
        });
        html = (injected && injected.result && injected.result.html) || "";
        text = (injected && injected.result && injected.result.text) || "";
      } catch (_) {
        // Reading the page failed — a PDF, a download, a page that refuses
        // injection. The landing URL alone is still worth reporting.
      }

      return {
        final_url: tab.url || url,
        html: html.slice(0, 400000),
        text: text.slice(0, 400000),
        via: "tab",
      };
    } finally {
      await chrome.windows.remove(win.id).catch(() => {});
    }
  });
}

/** Whether a failed fetch is worth reopening as a real page. */
function worthEscalating(message) {
  // 403/429 and network-level refusals are the shapes that mean "not like
  // this". A 404 is the page genuinely not being there, and reopening it in a
  // window would only cost the user a flicker to learn the same thing.
  return /HTTP (401|403|405|406|429|503)\b|Failed to fetch|NetworkError/i.test(message);
}

async function tabsAllowed() {
  const { useTabs } = await chrome.storage.local.get({ useTabs: true });
  return useTabs;
}

// ---------------------------------------------------------------------------
// Doing the work
// ---------------------------------------------------------------------------

/**
 * What this agent can actually run.
 *
 * `ping` earns its place by depending on nothing: it proves lease → execute →
 * report works without any website being reachable or any session being valid,
 * so a broken handler stays distinguishable from a broken protocol.
 */
const HANDLERS = {
  async ping(payload) {
    return { pong: true, echo: payload ?? {}, at: new Date().toISOString() };
  },

  /**
   * Fetch a JSON endpoint the server is walled out of.
   *
   * Reddit answers a datacenter IP with 403 Blocked — a categorical refusal
   * rather than a rate limit, so no amount of retrying from the VPS helps. From
   * here it is a browser on a home connection asking for a public page, which
   * is the entire premise of this queue.
   *
   * Cookies are omitted. These are public endpoints, and sending the user's
   * session to them would be an unnecessary widening of what a queued URL can
   * reach.
   */
  async fetch_json(payload) {
    const url = payload && payload.url;
    if (!url) throw new Error("fetch_json needs a url.");

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {
        method: "GET",
        // Cookies for the sites that need a session to answer at all, omitted
        // everywhere else. See SESSION_HOSTS — the decision is made here from
        // the URL rather than taken from the task, so a queued URL can never
        // ask for your cookies to be sent somewhere new.
        credentials: sendsCredentialsTo(url) ? "include" : "omit",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });

      const text = (await response.text()).slice(0, 2000000);

      if (!response.ok) {
        // The body, not just the status. A 403 is a block page, a rate limit,
        // or a demand to log in, and those have three different fixes — the
        // number alone sent us looking in the wrong place once already.
        const hint = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 300);
        const message =
          `HTTP ${response.status} from ${new URL(url).host}` + (hint ? ` — ${hint}` : "");

        // Same escalation as resolve_link: a JSON endpoint that refuses a
        // background fetch will usually render for a real page load, and
        // `innerText` on the result is the same document.
        if (worthEscalating(message) && (await tabsAllowed())) {
          const opened = await openInTab(url);
          try {
            return { status: 200, json: JSON.parse(opened.text), via: "tab" };
          } catch (_) {
            throw new Error(`${message} (also unreadable when opened as a page)`);
          }
        }
        throw new Error(message);
      }

      try {
        return { status: response.status, json: JSON.parse(text) };
      } catch (_) {
        // Not JSON after all — hand back the text so the server can say why
        // rather than failing with "unparseable" and nothing to look at.
        return { status: response.status, json: null, text: text.slice(0, 4000) };
      }
    } finally {
      clearTimeout(timer);
    }
  },

  /**
   * Follow an aggregator redirect to the employer's real apply page.
   *
   * The server tries this first and gets most of them. What reaches here is
   * what a datacenter IP could not follow — Indeed and Glassdoor answer those
   * with an interstitial or a challenge rather than a redirect. From here it is
   * an ordinary browser making an ordinary request, which is the entire point.
   *
   * `fetch` follows redirects on its own and `response.url` is where it landed,
   * so the hop chain needs no manual walking. The body comes back too: even
   * when the landing page is another aggregator and therefore not an apply
   * link, it often names the company's Greenhouse or Lever board, which the
   * server mines separately.
   */
  async resolve_link(payload) {
    const url = payload && payload.url;
    if (!url) throw new Error("resolve_link needs a url.");

    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(url, {
          method: "GET",
          redirect: "follow",
          // No cookies. Resolving a public redirect does not need the user's
          // sessions, and sending them to an arbitrary aggregator would be a
          // gratuitous widening of what this handler can leak.
          credentials: "omit",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status} from ${new URL(url).host}`);

        const contentType = response.headers.get("content-type") || "";
        // Cap the body. Some landing pages are enormous, and everything the
        // server mines out of one is in the markup near the top.
        const html = contentType.includes("html")
          ? (await response.text()).slice(0, 400000)
          : "";

        return { final_url: response.url, status: response.status, html };
      } finally {
        clearTimeout(timer);
      }
    } catch (error) {
      // Aggregators refuse the fetch shape, not this browser. Opening the URL
      // as a real page gets the redirect chain, the JavaScript and the
      // meta-refresh that a service-worker fetch never sees.
      const message = String(error && error.message ? error.message : error);
      if (!worthEscalating(message) || !(await tabsAllowed())) throw error;
      return await openInTab(url);
    }
  },
};

/**
 * The permission `resolve_link` needs: fetching arbitrary job sites.
 *
 * Deliberately not requested at install. Reaching your own server is one thing;
 * reading any page on the web is a materially larger ask, and it should be a
 * separate, revocable decision rather than something bundled into setup.
 */
const BROAD_HOSTS = { origins: ["https://*/*", "http://*/*"] };

/**
 * Hosts that get the browser's cookies on a queued fetch.
 *
 * Reddit refuses anonymous JSON outright — 403 whoever asks, residential IP or
 * not — so the only thing that makes the browser more useful than the server
 * here is the session you already have. That is the extension's entire premise,
 * and omitting cookies threw it away.
 *
 * Decided here from the URL rather than read out of the task, so that the
 * server can never queue a URL that causes your cookies to be sent somewhere
 * this list does not already name.
 */
const SESSION_HOSTS = ["reddit.com", "www.reddit.com", "old.reddit.com"];

function sendsCredentialsTo(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return SESSION_HOSTS.some((allowed) => host === allowed || host.endsWith(`.${allowed}`));
  } catch (_) {
    return false;
  }
}

/**
 * Whether arbitrary sites are reachable.
 *
 * Checked one origin at a time and satisfied by either. `permissions.contains`
 * with both listed demands both, and Chrome does not always end up holding both
 * after a request — so asking for the pair answers "false" while https is in
 * fact granted, and the agent then quietly stops claiming work it could do.
 * Everything queued here is https in practice.
 */
async function canReachTheWeb() {
  for (const origin of BROAD_HOSTS.origins) {
    try {
      if (await chrome.permissions.contains({ origins: [origin] })) return true;
    } catch (_) {
      /* asking about one origin failing should not veto the other */
    }
  }
  return false;
}

/**
 * What this agent will actually claim right now.
 *
 * Permission-aware on purpose: leasing a kind we cannot run would take the task
 * out of the queue only to fail it, three times, until it retires. Not asking
 * for it leaves it queued for an engine that can.
 */
async function supportedKinds() {
  const kinds = ["ping"];
  if (await canReachTheWeb()) kinds.push("resolve_link", "fetch_json");
  return kinds;
}

async function runTask(task) {
  const handler = HANDLERS[task.kind];
  if (!handler) throw new Error(`This agent cannot run ${task.kind} tasks.`);
  return await handler(task.payload);
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

let polling = false;

async function pollOnce() {
  // Alarms can overlap a chained poll that is still running. Two concurrent
  // loops would lease two batches and race each other's reports.
  if (polling) return;
  polling = true;

  try {
    const config = await getConfig();
    if (!config.enabled || !config.serverUrl || !config.token) return;

    const id = await agentId();
    const kinds = await supportedKinds();
    const { tasks } = await api("/api/agent/lease", {
      kinds,
      agent_id: id,
      max: 5,
      wait: 25,
    });

    // Recorded locally as well as sent, so the options page can show what this
    // install offers without waking the worker to ask.
    await setStatus({
      lastPoll: new Date().toISOString(),
      lastError: null,
      kinds,
    });
    if (!tasks || tasks.length === 0) return;

    let done = 0;
    for (const task of tasks) {
      try {
        const result = await runTask(task);
        await api(`/api/agent/tasks/${task.id}/result`, { result, agent_id: id });
        done += 1;
      } catch (error) {
        // Report the failure rather than dropping it. The server decides what
        // to do with it — but only this side knows whether a retry could ever
        // help, so a refusal is flagged as final. A 403 will be a 403 again,
        // and three identical rows bury whatever else failed that hour.
        const message = String(error && error.message ? error.message : error);
        await api(`/api/agent/tasks/${task.id}/fail`, {
          error: message,
          permanent: /HTTP 4(0[0-9]|1[0-8])\b/.test(message),
          agent_id: id,
        });
      }
    }
    await setStatus({ lastCompleted: done, lastTaskAt: new Date().toISOString() });

    // More work probably waits behind what we just drained, and the next alarm
    // is up to a minute away.
    setTimeout(() => pollOnce(), 0);
  } catch (error) {
    await setStatus({ lastError: String(error && error.message ? error.message : error) });
  } finally {
    polling = false;
  }
}

// ---------------------------------------------------------------------------
// Passive harvest
// ---------------------------------------------------------------------------

/**
 * Harvest reads job JSON from LinkedIn pages you visit anyway. Narrow on
 * purpose — one site, not the whole web — and registered at runtime rather than
 * declared in the manifest, so installing the extension does not ask for
 * LinkedIn access on behalf of a feature that is off.
 */
/* HARVEST_SITES is imported at the top of this file — see sites.js. */

/** Kept for the options page and for anything still reading one site. */
const HARVEST_HOSTS = { origins: ["https://www.linkedin.com/*"] };

function harvestScriptsFor(site) {
  return [
    {
      id: `jobapp-interceptor-${site.id}`,
      matches: site.matches,
      js: ["interceptor.js"],
      runAt: "document_start",
      // MAIN shares the page's globals, which is the only way to patch the
      // `fetch` the page itself calls. It also means no chrome.* here.
      world: "MAIN",
    },
    {
      id: `jobapp-relay-${site.id}`,
      matches: site.matches,
      js: ["relay.js"],
      runAt: "document_start",
    },
  ];
}

async function registeredIds(ids) {
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({ ids });
    return new Set(existing.map((s) => s.id));
  } catch (_) {
    return new Set();
  }
}

async function syncHarvestScripts() {
  const keys = Object.fromEntries(
    HARVEST_SITES.map((site) => [site.storageKey, false]),
  );
  const stored = await chrome.storage.local.get(keys);

  const allIds = HARVEST_SITES.flatMap((site) =>
    harvestScriptsFor(site).map((s) => s.id),
  );
  // The pre-multi-site ids, so an upgrade does not leave a stale pair
  // registered against LinkedIn forever.
  const legacyIds = ["jobapp-interceptor", "jobapp-relay"];
  await chrome.scripting
    .unregisterContentScripts({ ids: legacyIds })
    .catch(() => {});

  const already = await registeredIds(allIds);

  for (const site of HARVEST_SITES) {
    const scripts = harvestScriptsFor(site);
    const ids = scripts.map((s) => s.id);
    const wanted =
      Boolean(stored[site.storageKey]) &&
      (await chrome.permissions.contains({ origins: site.matches }));
    const registered = ids.every((id) => already.has(id));

    try {
      if (wanted && !registered) {
        // Unregister first: a half-registered pair from an interrupted run
        // would otherwise make register() throw on the duplicate id.
        await chrome.scripting.unregisterContentScripts({ ids }).catch(() => {});
        await chrome.scripting.registerContentScripts(scripts);
      } else if (!wanted && registered) {
        await chrome.scripting.unregisterContentScripts({ ids });
      }
    } catch (error) {
      await setStatus({
        lastError: `harvest scripts (${site.id}): ${error.message}`,
      });
    }
  }
}

// ---------------------------------------------------------------------------
// On-page overlay
// ---------------------------------------------------------------------------

/**
 * Job sites the overlay draws on. A list rather than a wildcard: it covers the
 * ATS boards where applications actually happen, and asking for those by name
 * is a smaller and more legible request than "every site you visit".
 */
const OVERLAY_MATCHES = [
  "https://www.linkedin.com/jobs/*",
  "https://boards.greenhouse.io/*",
  "https://job-boards.greenhouse.io/*",
  "https://jobs.lever.co/*",
  "https://jobs.ashbyhq.com/*",
  "https://*.myworkdayjobs.com/*",
  "https://apply.workable.com/*",
  "https://jobs.smartrecruiters.com/*",
  "https://*.recruitee.com/*",
];
const OVERLAY_HOSTS = { origins: OVERLAY_MATCHES };
const OVERLAY_SCRIPTS = [
  {
    id: "jobapp-overlay",
    matches: OVERLAY_MATCHES,
    js: ["overlay.js"],
    runAt: "document_idle",
  },
];

async function syncOverlayScripts() {
  const wanted =
    (await chrome.storage.local.get({ overlay: false })).overlay &&
    (await chrome.permissions.contains(OVERLAY_HOSTS));
  let registered = false;
  try {
    registered =
      (await chrome.scripting.getRegisteredContentScripts({ ids: ["jobapp-overlay"] }))
        .length > 0;
  } catch (_) {
    registered = false;
  }

  try {
    if (wanted && !registered) {
      await chrome.scripting
        .unregisterContentScripts({ ids: ["jobapp-overlay"] })
        .catch(() => {});
      await chrome.scripting.registerContentScripts(OVERLAY_SCRIPTS);
    } else if (!wanted && registered) {
      await chrome.scripting.unregisterContentScripts({ ids: ["jobapp-overlay"] });
    }
  } catch (error) {
    await setStatus({ lastError: `overlay scripts: ${error.message}` });
  }
}

/**
 * The overlay's only route to the server.
 *
 * It cannot call the API itself: the token lives in extension storage, and a
 * content script that held it would be handing a credential to whatever page it
 * is running on. So the panel asks, this fetches, and the token never enters
 * the page's process.
 */
async function overlayApi(path, body) {
  const config = await getConfig();
  if (!config.serverUrl || !config.token) {
    return { error: "Set your server URL and token in the extension options." };
  }
  try {
    const data = await api(path, body);
    return { data, serverUrl: config.serverUrl };
  } catch (error) {
    return { error: error.message, serverUrl: config.serverUrl };
  }
}

// ---------------------------------------------------------------------------
// Event reporting
// ---------------------------------------------------------------------------

/**
 * Tell the server what happened, and remember it here either way.
 *
 * Two destinations because they answer different questions. The server's copy
 * is the one that survives a reinstall and can be counted over weeks. The local
 * ring buffer is what the options page shows, and it is the only thing that
 * works when the server is the thing that is broken — which is exactly when
 * somebody opens the options page.
 *
 * Nothing here throws. Reporting that an autofill happened must never be able
 * to break the autofill.
 */
const EVENT_BUFFER_SIZE = 50;

async function reportEvent(kind, { url, ok = true, summary } = {}) {
  const entry = {
    kind,
    // The host, never the page. The server stores it that way for the same
    // reason: this is a diagnostic, not a browsing history.
    host: hostOf(url),
    ok,
    summary: summary || {},
    at: new Date().toISOString(),
  };

  try {
    const stored = (await chrome.storage.local.get("events")).events || [];
    await chrome.storage.local.set({
      events: [entry, ...stored].slice(0, EVENT_BUFFER_SIZE),
    });
  } catch (_) {
    /* storage full or unavailable; the server copy is still worth trying */
  }

  try {
    const config = await getConfig();
    if (!config.serverUrl || !config.token) return;
    await api("/api/agent/report", {
      agent_id: await agentId(),
      events: [entry],
    });
  } catch (_) {
    // Offline is the normal state for half the day. The local buffer already
    // has it, and losing one diagnostic event is not worth a retry queue.
  }
}

function hostOf(url) {
  if (!url) return null;
  try {
    return new URL(url).hostname || null;
  } catch (_) {
    return null;
  }
}

async function forwardHarvest(payload, sourceUrl) {
  const config = await getConfig();
  if (!config.serverUrl || !config.token) return;
  try {
    const counts = await api("/api/agent/harvest", {
      payload,
      source_url: sourceUrl,
      // So the server files the event under the browser it came from. Several
      // can be running, and "harvest stopped working" is usually only true of
      // one of them.
      agent_id: await agentId(),
    });
    if (counts && counts.found) {
      await setStatus({
        lastHarvest: new Date().toISOString(),
        lastHarvestFound: counts.found,
        lastHarvestNew: counts.inserted || 0,
      });
    }
  } catch (error) {
    await setStatus({ lastError: `harvest: ${error.message}` });
  }
}

function ensureAlarm() {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  syncHarvestScripts();
  syncOverlayScripts();
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  syncHarvestScripts();
  syncOverlayScripts();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) pollOnce();
});

// The options page asks for an immediate poll after you enable the agent, so
// that "on" and "working" are the same moment rather than a minute apart.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "harvest") {
    // Only from a tab we injected into. A message with no tab came from an
    // extension page, which has no business offering harvested jobs.
    if (!sender.tab) return false;
    forwardHarvest(message.payload, message.sourceUrl);
    return false;
  }
  if (message?.type === "sync-harvest") {
    Promise.all([syncHarvestScripts(), syncOverlayScripts()]).then(() =>
      sendResponse({ ok: true }),
    );
    return true;
  }
  if (message?.type === "overlay-api") {
    if (!sender.tab) return false;
    overlayApi(message.path, message.body).then(sendResponse);
    return true;
  }
  if (message?.type === "overlay-event") {
    // From the panel, which knows what it did — whether a fill matched
    // anything, whether the resume went in. The service worker cannot see any
    // of that, and neither could the server.
    if (!sender.tab) return false;
    reportEvent(message.kind, {
      url: sender.tab.url,
      ok: message.ok !== false,
      summary: message.summary,
    });
    return false;
  }
  if (message?.type === "poll-now") {
    pollOnce().then(() => sendResponse({ ok: true }));
    return true; // keep the channel open for the async reply
  }
  if (message?.type === "supported-kinds") {
    supportedKinds().then((kinds) => sendResponse({ kinds }));
    return true;
  }
  return false;
});

ensureAlarm();
syncHarvestScripts();
syncOverlayScripts();
