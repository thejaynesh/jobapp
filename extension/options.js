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
  harvest: document.getElementById("harvest"),
  harvestIndeed: document.getElementById("harvestIndeed"),
  harvestGlassdoor: document.getElementById("harvestGlassdoor"),
  harvestWorkday: document.getElementById("harvestWorkday"),
  useTabs: document.getElementById("useTabs"),
  overlay: document.getElementById("overlay"),
  save: document.getElementById("save"),
  test: document.getElementById("test"),
  message: document.getElementById("message"),
  agent: document.getElementById("s-agent"),
  poll: document.getElementById("s-poll"),
  error: document.getElementById("s-error"),
  kinds: document.getElementById("s-kinds"),
  harvestStatus: document.getElementById("s-harvest"),
  events: document.querySelector("#events tbody"),
};

/**
 * Reading arbitrary sites, which `resolve_link` needs and nothing else does.
 *
 * Kept separate from the server origin so that setting the agent up does not
 * silently buy access to every page on the web, and so turning it off later is
 * one click rather than a reinstall.
 */
const BROAD_HOSTS = { origins: ["https://*/*", "http://*/*"] };

/** Harvest needs one site, not the web. Asked for separately for that reason. */
const HARVEST_HOSTS = { origins: ["https://www.linkedin.com/*"] };

/**
 * Every site harvesting can read, one row each.
 *
 * A row per site rather than one wildcard: "read every job board you visit" is
 * a different thing to consent to than "read LinkedIn", so each is asked for
 * separately, shown separately, and given back separately. LinkedIn keeps the
 * original `harvest` storage key so existing installs need no migration.
 */
const HARVEST_SITES = [
  { key: "harvest", el: "harvest", origins: ["https://www.linkedin.com/*"] },
  { key: "harvestIndeed", el: "harvestIndeed", origins: ["https://*.indeed.com/*"] },
  {
    key: "harvestGlassdoor",
    el: "harvestGlassdoor",
    origins: ["https://*.glassdoor.com/*"],
  },
  {
    key: "harvestWorkday",
    el: "harvestWorkday",
    origins: ["https://*.myworkdayjobs.com/*"],
  },
];

/** Where the overlay draws. Named boards rather than a wildcard. */
const OVERLAY_HOSTS = {
  origins: [
    "https://www.linkedin.com/jobs/*",
    "https://boards.greenhouse.io/*",
    "https://job-boards.greenhouse.io/*",
    "https://jobs.lever.co/*",
    "https://jobs.ashbyhq.com/*",
    "https://*.myworkdayjobs.com/*",
    "https://apply.workable.com/*",
    "https://jobs.smartrecruiters.com/*",
    "https://*.recruitee.com/*",
  ],
};

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
    serverUrl: "", token: "", enabled: false, overlay: false,
    useTabs: true, agentId: "", status: {}, events: [],
    ...Object.fromEntries(HARVEST_SITES.map((site) => [site.key, false])),
  });
  els.serverUrl.value = stored.serverUrl;
  els.token.value = stored.token;
  els.enabled.checked = stored.enabled;
  els.agent.textContent = stored.agentId || "—";
  els.resolveLinks.checked = await chrome.permissions.contains(BROAD_HOSTS);
  // A ticked box with the permission revoked out from under it is a lie, so
  // each one is shown as the AND of the two.
  for (const site of HARVEST_SITES) {
    const box = els[site.el];
    if (!box) continue;
    box.checked =
      Boolean(stored[site.key]) &&
      (await chrome.permissions.contains({ origins: site.origins }));
  }
  els.overlay.checked =
    stored.overlay && (await chrome.permissions.contains(OVERLAY_HOSTS));
  els.useTabs.checked = stored.useTabs;
  els.harvestStatus.textContent = stored.status.lastHarvest
    ? `${new Date(stored.status.lastHarvest).toLocaleString()} ` +
      `(${stored.status.lastHarvestFound} found, ${stored.status.lastHarvestNew} new)`
    : "never";
  chrome.runtime.sendMessage({ type: "supported-kinds" }, (reply) => {
    // A dead service worker leaves no reply; that is not worth surfacing.
    if (chrome.runtime.lastError || !reply) return;
    els.kinds.textContent = (reply.kinds || []).join(", ") || "nothing";
  });
  els.poll.textContent = stored.status.lastPoll
    ? new Date(stored.status.lastPoll).toLocaleString()
    : "never";
  els.error.textContent = stored.status.lastError || "none";
  renderEvents(stored.events || []);
}


/**
 * The last few things this browser did.
 *
 * Built with DOM calls rather than innerHTML because every value in here —
 * hostnames, error messages — came off a page we do not control.
 */
function renderEvents(events) {
  if (!els.events) return;
  els.events.replaceChildren();
  if (!events.length) {
    const row = els.events.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 4;
    cell.textContent =
      "Nothing yet. Open the panel on a job page, or tick a harvest box above.";
    cell.className = "host";
    return;
  }
  for (const event of events) {
    const row = els.events.insertRow();
    if (event.ok === false) row.className = "failed";

    const when = row.insertCell();
    when.className = "when";
    when.textContent = new Date(event.at).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });

    const kind = row.insertCell();
    kind.className = "kind";
    kind.textContent = (event.kind || "").replace(/_/g, " ");

    const host = row.insertCell();
    host.className = "host";
    host.textContent = event.host || "";

    const summary = row.insertCell();
    summary.className = "host";
    summary.textContent = Object.entries(event.summary || {})
      .map(([key, value]) => `${key}=${value}`)
      .join(" ");
  }
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

  // Harvest, same shape, once per site: the permission follows the checkbox
  // both ways, and the content scripts follow the permission.
  for (const site of HARVEST_SITES) {
    const box = els[site.el];
    if (!box) continue;
    const hosts = { origins: site.origins };
    const held = await chrome.permissions.contains(hosts);
    if (box.checked && !held) {
      if (!(await chrome.permissions.request(hosts))) box.checked = false;
    } else if (!box.checked && held && !els.resolveLinks.checked) {
      // Broad access already covers these hosts, so only give one back when
      // it is not being kept alive by the other toggle anyway.
      await chrome.permissions.remove(hosts);
    }
  }
  const hasOverlay = await chrome.permissions.contains(OVERLAY_HOSTS);
  if (els.overlay.checked && !hasOverlay) {
    if (!(await chrome.permissions.request(OVERLAY_HOSTS))) {
      els.overlay.checked = false;
    }
  } else if (!els.overlay.checked && hasOverlay && !els.resolveLinks.checked) {
    await chrome.permissions.remove(OVERLAY_HOSTS);
  }

  await chrome.storage.local.set({
    ...Object.fromEntries(
      HARVEST_SITES.filter((site) => els[site.el]).map((site) => [
        site.key,
        els[site.el].checked,
      ]),
    ),
    overlay: els.overlay.checked,
    useTabs: els.useTabs.checked,
  });
  chrome.runtime.sendMessage({ type: "sync-harvest" }, () => {
    void chrome.runtime.lastError;
  });

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
