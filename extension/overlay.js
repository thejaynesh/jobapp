/**
 * The card that appears on a job posting: what we know, before you spend an
 * hour on it.
 *
 * Three questions, answered without leaving the page — have I seen this, what
 * did it score, did I already apply. All three are already in the database; the
 * only reason they were hard to reach is that they lived on a different tab.
 *
 * Everything is drawn inside a shadow root. Job sites ship aggressive global
 * CSS (`* { box-sizing }`, resets on every element, z-index wars), and a plain
 * injected div inherits all of it — a panel that looks right on Greenhouse
 * would be unreadable on Workday. A shadow root is the only way to be sure the
 * host page cannot reach in, and equally that this cannot leak out and break
 * the page it is sitting on.
 *
 * Nothing here runs until the panel is opened. On page load this only draws a
 * small button, because a content script that fires a request on every
 * navigation is a content script that gets uninstalled.
 */

(() => {
  if (window.__jobappOverlayInstalled) return;
  window.__jobappOverlayInstalled = true;

  const HOST_ID = "jobapp-overlay-host";

  const CSS = `
    :host { all: initial; }
    .launcher, .panel {
      position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
      font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
      color: #111;
    }
    .launcher {
      width: 40px; height: 40px; border-radius: 20px; border: none;
      background: #2563eb; color: #fff; font-size: 17px; cursor: pointer;
      box-shadow: 0 2px 10px rgba(0,0,0,.28);
    }
    .panel {
      width: 300px; background: #fff; border-radius: 10px; padding: 14px;
      box-shadow: 0 6px 28px rgba(0,0,0,.24); border: 1px solid #e5e7eb;
      max-height: 78vh; overflow-y: auto;
    }
    .head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
    .head strong { font-size: 13px; }
    .x { border: none; background: none; font-size: 17px; cursor: pointer; color: #666; line-height: 1; }
    .score { font-size: 26px; font-weight: 700; margin: 2px 0; }
    .muted { color: #666; font-size: 12px; }
    .row { margin: 8px 0; }
    .pill {
      display: inline-block; padding: 2px 7px; border-radius: 10px;
      font-size: 11px; font-weight: 600; margin: 2px 3px 2px 0;
    }
    .ok { background: #dcfce7; color: #14532d; }
    .warn { background: #fef3c7; color: #78350f; }
    .bad { background: #fee2e2; color: #7f1d1d; }
    .info { background: #e0e7ff; color: #1e3a8a; }
    button.action {
      width: 100%; padding: 7px; margin-top: 6px; border-radius: 6px;
      border: 1px solid #2563eb; background: #2563eb; color: #fff;
      font: inherit; font-weight: 600; cursor: pointer;
    }
    button.secondary { background: #fff; color: #2563eb; }
    button.action:disabled { opacity: .55; cursor: default; }
    a { color: #2563eb; }
  `;

  let root = null;

  function mount() {
    const host = document.createElement("div");
    host.id = HOST_ID;
    // Shadow, closed: the page has no legitimate reason to reach in, and an
    // open root is one querySelector away from a job site restyling this.
    root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = CSS;
    root.append(style);
    document.documentElement.append(host);
    showLauncher();
  }

  function clear() {
    root.querySelectorAll(".launcher, .panel").forEach((n) => n.remove());
  }

  function showLauncher() {
    clear();
    const button = document.createElement("button");
    button.className = "launcher";
    button.textContent = "J";
    button.title = "JobApp";
    button.addEventListener("click", open);
    root.append(button);
  }

  function panel(title) {
    clear();
    const box = document.createElement("div");
    box.className = "panel";
    const head = document.createElement("div");
    head.className = "head";
    const label = document.createElement("strong");
    label.textContent = title;
    const close = document.createElement("button");
    close.className = "x";
    close.textContent = "×";
    close.addEventListener("click", showLauncher);
    head.append(label, close);
    box.append(head);
    root.append(box);
    return box;
  }

  function line(parent, text, className = "muted") {
    const div = document.createElement("div");
    div.className = className;
    div.textContent = text;
    parent.append(div);
    return div;
  }

  function pill(parent, text, kind) {
    const span = document.createElement("span");
    span.className = `pill ${kind}`;
    span.textContent = text;
    parent.append(span);
  }

  async function ask(path, body) {
    return await new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "overlay-api", path, body }, (reply) => {
        if (chrome.runtime.lastError) {
          resolve({ error: chrome.runtime.lastError.message });
          return;
        }
        resolve(reply || { error: "No response from the extension." });
      });
    });
  }

  async function open() {
    const box = panel("JobApp");
    line(box, "Checking…");

    const reply = await ask(
      `/api/agent/job-context?url=${encodeURIComponent(location.href)}`,
    );
    if (reply.error) {
      const box2 = panel("JobApp");
      line(box2, reply.error, "muted");
      return;
    }
    render(reply.data || {}, reply.serverUrl || "");
  }

  function render(data, serverUrl) {
    const box = panel("JobApp");

    if (!data.known) {
      line(box, "Not in your tracker yet.");
      addPrepare(box, serverUrl, "Save and write documents");
      return;
    }

    const job = data.job || {};
    line(box, `${job.title || "This role"} — ${job.company || ""}`, "muted");

    if (job.score !== null && job.score !== undefined) {
      const score = document.createElement("div");
      score.className = "score";
      score.textContent = `${job.score}`;
      box.append(score);
      line(
        box,
        job.matched_by === "llm" ? "match score (model)" : "match score (keywords)",
      );
    }

    const flags = document.createElement("div");
    flags.className = "row";

    // The application state is the thing worth seeing first: it is the one
    // that means "stop reading, you already did this".
    const application = data.application;
    if (application && application.status && application.status !== "not_applied") {
      pill(flags, `already ${application.status}`, "info");
    } else if (application) {
      pill(flags, "application open", "info");
    }

    if (job.status === "filtered_out") {
      pill(flags, job.filter_reason === "restricted" ? "US citizens only" : "filtered out", "bad");
    }
    if (job.sponsorship_direction === "no_sponsorship") {
      pill(flags, "no sponsorship", "warn");
    } else if (job.sponsorship_direction === "sponsors") {
      pill(flags, "sponsors visas", "ok");
    }
    if (flags.children.length) box.append(flags);

    if (job.filter_detail) line(box, job.filter_detail);
    if (job.sponsorship_note) line(box, `“${job.sponsorship_note}”`);

    if ((job.matched_skills || []).length) {
      const row = document.createElement("div");
      row.className = "row";
      job.matched_skills.forEach((skill) => pill(row, skill, "ok"));
      (job.missing_skills || []).forEach((skill) => pill(row, skill, "warn"));
      box.append(row);
    }

    if (application) {
      addLink(box, serverUrl, data.path, "Open in JobApp");
    } else {
      addPrepare(box, serverUrl, "Write documents for this");
    }
  }

  function addLink(box, serverUrl, path, label) {
    if (!serverUrl || !path) return;
    const button = document.createElement("button");
    button.className = "action secondary";
    button.textContent = label;
    button.addEventListener("click", () => {
      window.open(new URL(path, serverUrl).toString(), "_blank", "noopener");
    });
    box.append(button);
  }

  function addPrepare(box, serverUrl, label) {
    const button = document.createElement("button");
    button.className = "action";
    button.textContent = label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Working…";
      const reply = await ask("/api/agent/prepare", {
        url: location.href,
        posting: readPosting(),
      });
      const data = reply.data || {};
      if (reply.error || !data.ok) {
        button.textContent = label;
        button.disabled = false;
        line(box, reply.error || data.detail || "That did not work.");
        return;
      }
      button.remove();
      line(
        box,
        data.generating
          ? "Saved. Documents are being written."
          : "Saved. It already had an application open.",
      );
      addLink(box, serverUrl, data.path, "Open in JobApp");
    });
    box.append(button);
  }

  /**
   * Title and company off the page, for a posting the pipeline never fetched.
   *
   * This is the one place selectors are unavoidable — there is no API response
   * to read on an arbitrary employer's careers page. It is best-effort by
   * design: the fields feed a "save this" button the user just pressed, so
   * getting them wrong costs a correction, not a silent bad record. Ordered
   * from structured data outward, because JSON-LD is both the most reliable and
   * the most common on ATS pages.
   */
  function readPosting() {
    const posting = { title: "", company: "", location: "", description: "" };

    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(node.textContent);
        const entries = Array.isArray(parsed) ? parsed : [parsed];
        for (const entry of entries) {
          if (!entry || entry["@type"] !== "JobPosting") continue;
          posting.title = entry.title || "";
          posting.company =
            (entry.hiringOrganization && entry.hiringOrganization.name) || "";
          const place = entry.jobLocation && entry.jobLocation.address;
          posting.location = place
            ? [place.addressLocality, place.addressRegion].filter(Boolean).join(", ")
            : "";
          posting.description = (entry.description || "")
            .replace(/<[^>]+>/g, " ")
            .slice(0, 20000);
          return posting;
        }
      } catch (_) {
        /* malformed JSON-LD is common; fall through to the guesses below */
      }
    }

    posting.title =
      document.querySelector("h1")?.textContent?.trim() ||
      document.title.split(/[|\-–]/)[0].trim();
    posting.company =
      document.querySelector('meta[property="og:site_name"]')?.content ||
      location.hostname.replace(/^(www|jobs|boards|careers|apply)\./, "").split(".")[0];
    posting.description = (document.body.innerText || "").slice(0, 20000);
    return posting;
  }

  if (document.documentElement) mount();
})();
