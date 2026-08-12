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

async function canReachTheWeb() {
  try {
    return await chrome.permissions.contains(BROAD_HOSTS);
  } catch (_) {
    return false;
  }
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
  if (await canReachTheWeb()) kinds.push("resolve_link");
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
    const { tasks } = await api("/api/agent/lease", {
      kinds: await supportedKinds(),
      agent_id: id,
      max: 5,
      wait: 25,
    });

    await setStatus({ lastPoll: new Date().toISOString(), lastError: null });
    if (!tasks || tasks.length === 0) return;

    let done = 0;
    for (const task of tasks) {
      try {
        const result = await runTask(task);
        await api(`/api/agent/tasks/${task.id}/result`, { result, agent_id: id });
        done += 1;
      } catch (error) {
        // Report the failure rather than dropping it. The server decides
        // whether that means a retry or a dead end; this side does not guess.
        await api(`/api/agent/tasks/${task.id}/fail`, {
          error: String(error && error.message ? error.message : error),
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
const HARVEST_HOSTS = { origins: ["https://www.linkedin.com/*"] };
const HARVEST_SCRIPTS = [
  {
    id: "jobapp-interceptor",
    matches: ["https://www.linkedin.com/*"],
    js: ["interceptor.js"],
    runAt: "document_start",
    // MAIN shares the page's globals, which is the only way to patch the
    // `fetch` the page itself calls. It also means no chrome.* here.
    world: "MAIN",
  },
  {
    id: "jobapp-relay",
    matches: ["https://www.linkedin.com/*"],
    js: ["relay.js"],
    runAt: "document_start",
  },
];

async function harvestRegistered() {
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({
      ids: HARVEST_SCRIPTS.map((s) => s.id),
    });
    return existing.length === HARVEST_SCRIPTS.length;
  } catch (_) {
    return false;
  }
}

async function syncHarvestScripts() {
  const wanted =
    (await chrome.storage.local.get({ harvest: false })).harvest &&
    (await chrome.permissions.contains(HARVEST_HOSTS));
  const registered = await harvestRegistered();

  try {
    if (wanted && !registered) {
      // Unregister first: a half-registered pair from an interrupted run would
      // otherwise make register() throw on the duplicate id.
      await chrome.scripting
        .unregisterContentScripts({ ids: HARVEST_SCRIPTS.map((s) => s.id) })
        .catch(() => {});
      await chrome.scripting.registerContentScripts(HARVEST_SCRIPTS);
    } else if (!wanted && registered) {
      await chrome.scripting.unregisterContentScripts({
        ids: HARVEST_SCRIPTS.map((s) => s.id),
      });
    }
  } catch (error) {
    await setStatus({ lastError: `harvest scripts: ${error.message}` });
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

async function forwardHarvest(payload, sourceUrl) {
  const config = await getConfig();
  if (!config.serverUrl || !config.token) return;
  try {
    const counts = await api("/api/agent/harvest", { payload, source_url: sourceUrl });
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
  syncHarvestScripts();
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
syncHarvestScripts();
syncOverlayScripts();
  syncHarvestScripts();
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
