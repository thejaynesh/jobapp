/**
 * Settings, and the "does this actually work" button.
 *
 * The host permission is requested here rather than declared in the manifest.
 * The server address is whatever domain the user deployed to, so it cannot be
 * known in advance — and declaring a wildcard over all of https up front would
 * mean an install prompt claiming access to every site on the internet, in
 * order to talk to one. Requesting the specific origin at save time is both
 * narrower and honest.
 */

const els = {
  serverUrl: document.getElementById("serverUrl"),
  token: document.getElementById("token"),
  enabled: document.getElementById("enabled"),
  resolveLinks: document.getElementById("resolveLinks"),
  save: document.getElementById("save"),
  test: document.getElementById("test"),
  message: document.getElementById("message"),
  agent: document.getElementById("s-agent"),
  poll: document.getElementById("s-poll"),
  error: document.getElementById("s-error"),
  kinds: document.getElementById("s-kinds"),
};

/**
 * Reading arbitrary sites, which `resolve_link` needs and nothing else does.
 *
 * Kept separate from the server origin so that setting the agent up does not
 * silently buy access to every page on the web, and so turning it off later is
 * one click rather than a reinstall.
 */
const BROAD_HOSTS = { origins: ["https://*/*", "http://*/*"] };

function say(text, kind = "ok") {
  els.message.textContent = text;
  els.message.className = kind;
}

function normalizeUrl(raw) {
  const trimmed = (raw || "").trim().replace(/\/+$/, "");
  if (!trimmed) return "";
  // A bare domain is what people type; assume https rather than rejecting it.
  return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
}

function originPattern(url) {
  const parsed = new URL(url);
  return `${parsed.protocol}//${parsed.host}/*`;
}

async function load() {
  const stored = await chrome.storage.local.get({
    serverUrl: "", token: "", enabled: false, agentId: "", status: {},
  });
  els.serverUrl.value = stored.serverUrl;
  els.token.value = stored.token;
  els.enabled.checked = stored.enabled;
  els.agent.textContent = stored.agentId || "—";
  els.resolveLinks.checked = await chrome.permissions.contains(BROAD_HOSTS);
  chrome.runtime.sendMessage({ type: "supported-kinds" }, (reply) => {
    // A dead service worker leaves no reply; that is not worth surfacing.
    if (chrome.runtime.lastError || !reply) return;
    els.kinds.textContent = (reply.kinds || []).join(", ") || "nothing";
  });
  els.poll.textContent = stored.status.lastPoll
    ? new Date(stored.status.lastPoll).toLocaleString()
    : "never";
  els.error.textContent = stored.status.lastError || "none";
}

async function save() {
  const serverUrl = normalizeUrl(els.serverUrl.value);
  const token = els.token.value.trim();

  if (!serverUrl || !token) {
    say("Both the server URL and the token are needed.", "err");
    return false;
  }
  let pattern;
  try {
    pattern = originPattern(serverUrl);
  } catch (_) {
    say("That server URL is not a valid address.", "err");
    return false;
  }

  const granted = await chrome.permissions.request({ origins: [pattern] });
  if (!granted) {
    say("Permission to reach that server was declined, so nothing was saved.", "err");
    return false;
  }

  await chrome.storage.local.set({ serverUrl, token, enabled: els.enabled.checked });
  els.serverUrl.value = serverUrl;

  // Broad host access follows the checkbox in both directions. Requesting must
  // happen in this click handler — Chrome refuses a permission prompt that is
  // not a direct response to a gesture — and removing it here means the toggle
  // is a real switch rather than a one-way door.
  const hasBroad = await chrome.permissions.contains(BROAD_HOSTS);
  if (els.resolveLinks.checked && !hasBroad) {
    const granted = await chrome.permissions.request(BROAD_HOSTS);
    if (!granted) {
      els.resolveLinks.checked = false;
      say("Saved, but link resolving stays off without permission to read sites.", "err");
      return true;
    }
  } else if (!els.resolveLinks.checked && hasBroad) {
    await chrome.permissions.remove(BROAD_HOSTS);
  }

  if (els.enabled.checked) {
    // Start now rather than up to a minute from now, so switching it on and
    // seeing it work are the same moment.
    chrome.runtime.sendMessage({ type: "poll-now" });
  }
  say("Saved.");
  await load();
  return true;
}

async function test() {
  if (!(await save())) return;

  const { serverUrl, token } = await chrome.storage.local.get({ serverUrl: "", token: "" });
  els.test.disabled = true;
  try {
    const response = await fetch(new URL("/api/agent/hello", serverUrl).toString(), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (_) { /* status line is all we have */ }
      say(detail, "err");
      return;
    }
    const info = await response.json();
    const queue = info.queue || {};
    say(
      `Connected. Queue: ${queue.queued || 0} waiting, ${queue.leased || 0} in progress, ` +
      `${queue.done || 0} done.`
    );
  } catch (error) {
    // A network-level failure here is almost always the address, not the token
    // — a wrong token would have answered with a 401.
    say(`Could not reach the server: ${error.message}`, "err");
  } finally {
    els.test.disabled = false;
  }
}

els.save.addEventListener("click", save);
els.test.addEventListener("click", test);
load();
