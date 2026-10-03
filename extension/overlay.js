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

  const PANEL_CSS = `
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
    .draft { border-top: 1px solid #e5e7eb; margin-top: 10px; padding-top: 8px; }
    .draft .q { font-weight: 600; font-size: 12px; margin-bottom: 4px; }
    .draft textarea {
      width: 100%; box-sizing: border-box; min-height: 130px; resize: vertical;
      font: inherit; font-size: 12px; line-height: 1.45; color: #111;
      border: 1px solid #d1d5db; border-radius: 6px; padding: 6px;
    }
  `;

  let root = null;

  function mount() {
    const host = document.createElement("div");
    host.id = HOST_ID;
    // Shadow, closed: the page has no legitimate reason to reach in, and an
    // open root is one querySelector away from a job site restyling this.
    root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = PANEL_CSS;
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

  /**
   * Whether this script can still reach the extension it came from.
   *
   * Reloading an extension does not remove the content scripts it already
   * injected: they stay in every open tab, running, with the connection back
   * to the extension severed. `chrome.runtime.id` is `undefined` from that
   * moment, and any `sendMessage` throws "Extension context invalidated".
   *
   * That throw is synchronous, which is what makes it worth its own check.
   * `chrome.runtime.lastError` only ever reports a *delivered* call that found
   * no receiver — here the call never leaves, so the callback is not run and
   * the error escapes past every handler written around the reply.
   */
  function connected() {
    try {
      return Boolean(chrome.runtime && chrome.runtime.id);
    } catch (_) {
      return false;
    }
  }

  const RELOADED = "The extension was reloaded — refresh this page to use the panel.";

  async function ask(path, body, timeoutMs) {
    if (!connected()) return { error: RELOADED };
    return await new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: "overlay-api", path, body, timeoutMs }, (reply) => {
          if (chrome.runtime.lastError) {
            resolve({ error: chrome.runtime.lastError.message });
            return;
          }
          resolve(reply || { error: "No response from the extension." });
        });
      } catch (_) {
        // Invalidated between the check above and the call. Resolving keeps
        // this a message in the panel rather than a promise that never
        // settles, which is what the caller is awaiting.
        resolve({ error: RELOADED });
      }
    });
  }

  /**
   * Tell the service worker what just happened here.
   *
   * Fire and forget. The panel is the only thing that knows whether a fill
   * matched anything or whether the resume went in — the worker cannot see the
   * page, and the server only ever sees the calls the panel chose to make, so
   * a fill that recognised nothing was previously invisible from both ends.
   */
  function note(kind, summary, ok = true) {
    if (!connected()) return;
    try {
      chrome.runtime.sendMessage({ type: "overlay-event", kind, ok, summary });
    } catch (_) {
      /* the worker is asleep or the extension reloaded; not worth a retry */
    }
  }

  async function open() {
    const box = panel("JobApp");
    line(box, "Checking…");

    const reply = await ask(
      `/api/agent/job-context?url=${encodeURIComponent(location.href)}`,
    );
    note("overlay_open", { known: Boolean((reply.data || {}).known) },
         !reply.error);
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
      addFillButton(box);
      addDraftButton(box);
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
    if (job.sponsorship_direction === "negative") {
      pill(flags, "no sponsorship", "warn");
    } else if (job.sponsorship_direction === "positive") {
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

    addFillButton(box);
    addDraftButton(box);
    addResumeButton(box);
    addAppliedButton(box, application);
  }

  function addFillButton(box) {
    if (!looksLikeAForm()) return;
    const button = document.createElement("button");
    button.className = "action secondary";
    button.textContent = "Fill this form";
    button.addEventListener("click", () => fillForm(box, button));
    box.append(button);
    line(
      box,
      "Fills what it recognises and stops. It never submits — you read it and press apply.",
    );
    addRememberButton(box);
  }

  /**
   * Put the tailored resume into the form's file input.
   *
   * A content script genuinely can do this — build a `File`, put it in a
   * `DataTransfer`, assign `input.files`, dispatch `change` — and that is the
   * one part of an application that autofill could never reach. What it cannot
   * do is get the bytes: the PDF is behind the agent token, which lives in the
   * background worker and has no business being handed to an employer's page.
   * So the worker fetches it and passes the bytes through.
   */
  function addResumeButton(box) {
    if (!fileInputs().length) return;
    const button = document.createElement("button");
    button.className = "action secondary";
    button.textContent = "Attach resume";
    button.addEventListener("click", () => attachResume(box, button));
    box.append(button);
  }

  function fileInputs() {
    return Array.from(document.querySelectorAll('input[type="file"]')).filter(
      (field) =>
        !field.disabled &&
        // Already answered: replacing an upload the user made by hand is the
        // same mistake as overwriting a filled text field.
        !(field.files && field.files.length),
    );
    // Deliberately no visibility test. A styled upload control hides the real
    // input behind a label and a custom button on most ATSes, so requiring a
    // bounding box would reject the common case rather than the wrong one.
  }

  /** The input that wants a resume, or null when it cannot be told. */
  function resumeInput() {
    const inputs = fileInputs();
    if (!inputs.length) return null;

    const wanted = /(resume|résumé|cv\b|curriculum)/i;
    const unwanted = /(cover|letter|transcript|portfolio|photo|certificate|reference)/i;
    const named = inputs.filter((field) => {
      const haystack = describe(field) + " " + (field.getAttribute("accept") || "");
      return wanted.test(haystack) && !unwanted.test(haystack);
    });
    if (named.length === 1) return named[0];
    if (named.length > 1) return null;

    // Nothing said "resume". One unlabelled upload on an application form is
    // almost always it; several are a guess, and a cover letter filed as a
    // resume is worse than an empty slot the user fills themselves.
    const unclaimed = inputs.filter((field) => !unwanted.test(describe(field)));
    return unclaimed.length === 1 ? unclaimed[0] : null;
  }

  async function attachResume(box, button) {
    const field = resumeInput();
    if (!field) {
      note("attach_resume", { reason: "no unambiguous file input" }, false);
      line(
        box,
        "There is more than one upload on this page and none of them says " +
          "which is the resume — attach it yourself so it goes in the right slot.",
      );
      return;
    }

    button.disabled = true;
    button.textContent = "Fetching…";
    const reply = await ask(
      `/api/agent/resume?url=${encodeURIComponent(location.href)}`,
    );
    button.disabled = false;
    button.textContent = "Attach resume";

    const data = reply.data || {};
    if (reply.error || !data.ok) {
      note("attach_resume", { reason: reply.error || data.detail }, false);
      line(box, reply.error || data.detail || "Could not fetch the resume.");
      return;
    }

    try {
      const transfer = new DataTransfer();
      transfer.items.add(
        new File([decodeBase64(data.data)], data.filename || "resume.pdf", {
          type: data.content_type || "application/pdf",
        }),
      );
      field.files = transfer.files;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (error) {
      // Some forms make the input readonly or intercept assignment. Saying so
      // beats a button that reports success over an empty slot.
      note("attach_resume", { reason: `refused: ${error.message}` }, false);
      line(box, `This form would not accept the file (${error.message}).`);
      return;
    }
    note("attach_resume", { size: data.size });
    line(box, `Attached ${data.filename}. Check it appears on the form.`);
  }

  function decodeBase64(text) {
    const binary = atob(text || "");
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  /**
   * Mark it applied from the page you applied on.
   *
   * The moment Submit is pressed is the only moment the user knows for certain
   * that they applied, and it is the moment they are furthest from the
   * tracker. Every application marked days later, or never, is that gap.
   */
  function addAppliedButton(box, application) {
    if (!application) return;
    if (application.status && application.status !== "not_applied") return;

    const button = document.createElement("button");
    button.className = "action secondary";
    button.textContent = "Mark applied";
    const confirmation = autofill()?.receipt();
    if (confirmation) {
      line(box, "This page shows an application confirmation. Verify it is for this role, then mark applied.");
      button.textContent = "Confirm applied";
    }
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Saving…";
      const reply = await ask("/api/agent/mark-applied", { url: location.href });
      const data = reply.data || {};
      if (reply.error || !data.ok) {
        button.disabled = false;
        button.textContent = "Mark applied";
        note("mark_applied", { reason: reply.error || data.detail }, false);
        line(box, reply.error || data.detail || "That did not work.");
        return;
      }
      note("mark_applied", { changed: Boolean(data.changed) });
      button.remove();
      line(box, data.changed ? "Marked applied." : data.detail || "Already marked.");
    });
    box.append(button);
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
        note("prepare", { reason: reply.error || data.detail }, false);
        line(box, reply.error || data.detail || "That did not work.");
        return;
      }
      note("prepare", { generating: Boolean(data.generating) });
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

  // -------------------------------------------------------------------------
  // Autofill — the matching and typing live in autofill.js
  // -------------------------------------------------------------------------

  const autofill = () => globalThis.JobAppAutofill;

  function describe(field) {
    return autofill() ? autofill().describe(field) : "";
  }

  function looksLikeAForm() {
    return Boolean(autofill()) && autofill().looksLikeAForm();
  }

  let stopWatching = null;

  function summarize(report) {
    const parts = [];
    const profile = report.filled.length - report.remembered - report.declined;
    if (profile > 0) parts.push(`${profile} from your profile`);
    if (report.remembered) parts.push(`${report.remembered} you answered before`);
    if (report.declined) parts.push(`${report.declined} self-identification declined`);
    return parts.join(", ");
  }

  async function fillForm(box, button) {
    button.disabled = true;
    button.textContent = "Filling…";

    const reply = await ask("/api/agent/autofill-fields?site=" + encodeURIComponent(location.href));
    if (reply.error) {
      button.disabled = false;
      button.textContent = "Fill this form";
      line(box, reply.error);
      return;
    }

    const values = reply.data || {};
    const report = await autofill().fill(values);

    button.disabled = false;
    button.textContent = "Fill this form";
    // The number that says whether autofill is working on this site. A fill
    // that recognised two fields out of fifteen looks identical to a good one
    // from the server's side, because both make exactly one call.
    note(
      "autofill",
      { filled: report.filled.length, skipped: report.skipped.length,
        remembered: report.remembered, fields: report.filled },
      report.filled.length > 0,
    );
    line(
      box,
      report.filled.length
        ? `Verified ${report.filled.length} filled values (${summarize(report)}). Outlined in blue — check them, then submit yourself.`
        : "Nothing matched. Either the fields are already filled, or this form names them in a way I do not recognise.",
    );
    if (report.skipped.length) {
      line(
        box,
        `Needs your review: ${report.skipped.join(", ")}. An option did not match, a value did not persist, or the form reported a validation error.`,
      );
    }
    const unknown = (report.fields || []).filter((field) => field.status === "needs_input");
    if (unknown.length) line(box, `${unknown.length} fields need your input: ` + unknown.slice(0, 6).map((field) => field.question || field.key).join("; "));
    if (report.checkpoint?.resumed) line(box, "Resumed this form's checkpoint; existing answers were preserved and new fills were verified.");

    // Multi-step forms (Workday) draw the next step into the same page, so
    // keep filling what appears — empty fields only — for a while.
    if (stopWatching) stopWatching();
    const progress = line(box, "Watching for the next step of this form.");
    let later = 0;
    stopWatching = autofill().watch(values, (more) => {
      later += more.filled.length;
      progress.textContent = `Filled ${later} more as the form went on.`;
      note("autofill", { filled: more.filled.length, skipped: more.skipped.length,
                         remembered: more.remembered, fields: more.filled, step: true },
           more.filled.length > 0);
    });
  }

  /**
   * Remember what the user typed into questions nothing else could answer, so
   * the next form that asks in the same words is filled the same way. Pressed,
   * never automatic: the answers go to the user's own server, and only when
   * they say so.
   */
  function addRememberButton(box) {
    if (!looksLikeAForm()) return;
    const button = document.createElement("button");
    button.className = "action secondary";
    button.textContent = "Remember my answers";
    button.addEventListener("click", async () => {
      const answers = autofill().collectAnswers();
      if (!answers.length) {
        line(box, "No answers of your own on this page to remember yet.");
        return;
      }
      button.disabled = true;
      button.textContent = "Saving…";
      const reply = await ask("/api/agent/remember-answers", { answers, site: location.href });
      button.disabled = false;
      button.textContent = "Remember my answers";
      const saved = (reply.data || {}).saved;
      note("remember_answers", { offered: answers.length, saved: saved || 0 }, !reply.error);
      line(
        box,
        reply.error ||
          `Remembered ${saved} answer${saved === 1 ? "" : "s"}. The next form that ` +
          "asks the same question gets the same answer; your profile lists them.",
      );
    });
    box.append(button);
  }

  // -------------------------------------------------------------------------
  // Drafted answers to the long questions
  // -------------------------------------------------------------------------

  // A writing model can take most of a minute for one answer.
  const DRAFT_TIMEOUT_MS = 90000;

  /**
   * Offer drafts for the long questions on the form ("Why do you want to work
   * here?"). One request per question, in order, each shown in an editable box
   * as it arrives. Nothing goes into the form until "Put in form" is pressed
   * on a draft the user has read — and never over text they typed themselves.
   */
  function addDraftButton(box) {
    if (!autofill() || !autofill().longQuestions) return;
    const labelFor = (count) => `Draft answers to ${count} long question${count === 1 ? "" : "s"}`;
    const count = autofill().longQuestions().length;
    if (!count) return;
    const button = document.createElement("button");
    button.className = "action secondary";
    button.textContent = labelFor(count);
    button.addEventListener("click", async () => {
      button.disabled = true;
      const posting = readPosting();
      const questions = autofill().longQuestions();
      for (const [index, item] of questions.entries()) {
        button.textContent = `Drafting ${index + 1} of ${questions.length}…`;
        await draftOne(box, item, posting);
      }
      // Counted again: an answer put in the form is no longer waiting.
      const left = autofill().longQuestions().length;
      button.textContent = left ? labelFor(left) : "No long questions left";
      button.disabled = !left;
    });
    box.append(button);
  }

  async function draftOne(box, item, posting) {
    const card = document.createElement("div");
    card.className = "draft";
    const question = document.createElement("div");
    question.className = "q";
    question.textContent = item.question.length > 160 ? `${item.question.slice(0, 157)}…` : item.question;
    card.append(question);
    const status = line(card, "Drafting…");
    box.append(card);

    const reply = await ask("/api/agent/draft-answer", {
      url: location.href,
      question: item.question,
      max_chars: item.maxChars,
      posting,
    }, DRAFT_TIMEOUT_MS);
    const data = reply.data || {};
    if (reply.error || !data.ok) {
      status.textContent = data.declaration
        ? `Left for you: ${data.detail}`
        : reply.error || data.detail || "No draft this time.";
      note("draft_answer", { drafted: false, declaration: Boolean(data.declaration) },
           Boolean(data.declaration));
      return;
    }
    status.remove();

    const editor = document.createElement("textarea");
    editor.value = data.answer;
    card.append(editor);
    const meta = line(card, "");
    const count = () => {
      const words = editor.value.trim().split(/\s+/).filter(Boolean).length;
      meta.textContent = item.maxChars
        ? `${words} words · ${editor.value.length} of ${item.maxChars} characters`
        : `${words} words`;
    };
    count();
    editor.addEventListener("input", count);
    if ((data.unsupported_figures || []).length) {
      line(card, `Check these figures — they are not in your profile or the posting: ` +
                 `${data.unsupported_figures.join(", ")}.`);
    }

    const put = document.createElement("button");
    put.className = "action";
    put.textContent = "Put in form";
    put.addEventListener("click", () => {
      const text = editor.value.trim();
      if (!text) return;
      if (item.maxChars && text.length > item.maxChars) {
        meta.textContent = `Over the form's ${item.maxChars}-character limit — shorten it first.`;
        return;
      }
      const edited = text !== data.answer.trim();
      if (!autofill().put(item.field, text)) {
        line(card, "That box has text in it now, so it was left alone — copy this in by hand if you want it.");
        note("draft_answer", { drafted: true, put: false, reason: "field not empty" }, false);
        return;
      }
      note("draft_answer", { drafted: true, put: true, edited });
      editor.remove();
      put.remove();
      discard.remove();
      meta.textContent = "In the form, outlined in blue. Read it there before you submit.";
    });
    const discard = document.createElement("button");
    discard.className = "action secondary";
    discard.textContent = "Discard";
    discard.addEventListener("click", () => {
      note("draft_answer", { drafted: true, put: false });
      card.remove();
    });
    card.append(put, discard);
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
