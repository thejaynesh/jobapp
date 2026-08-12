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
 * Only `ping` for now, deliberately: it proves the whole round trip — lease,
 * execute, report — without depending on any website being reachable or any
 * session being valid. When the real handlers land, a broken one is then
 * distinguishable from a broken protocol.
 */
const HANDLERS = {
  async ping(payload) {
    return { pong: true, echo: payload ?? {}, at: new Date().toISOString() };
  },
};

function supportedKinds() {
  return Object.keys(HANDLERS);
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
      kinds: supportedKinds(),
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

function ensureAlarm() {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
}

chrome.runtime.onInstalled.addListener(ensureAlarm);
chrome.runtime.onStartup.addListener(ensureAlarm);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) pollOnce();
});

// The options page asks for an immediate poll after you enable the agent, so
// that "on" and "working" are the same moment rather than a minute apart.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "poll-now") {
    pollOnce().then(() => sendResponse({ ok: true }));
    return true; // keep the channel open for the async reply
  }
  if (message?.type === "supported-kinds") {
    sendResponse({ kinds: supportedKinds() });
  }
  return false;
});

ensureAlarm();
