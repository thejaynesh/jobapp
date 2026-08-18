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
      addFillButton(box);
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

    addFillButton(box);
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
      line(box, `This form would not accept the file (${error.message}).`);
      return;
    }
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
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Saving…";
      const reply = await ask("/api/agent/mark-applied", { url: location.href });
      const data = reply.data || {};
      if (reply.error || !data.ok) {
        button.disabled = false;
        button.textContent = "Mark applied";
        line(box, reply.error || data.detail || "That did not work.");
        return;
      }
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

  // -------------------------------------------------------------------------
  // Autofill
  // -------------------------------------------------------------------------

  /**
   * Which profile value belongs in a field, worked out from how it is labelled.
   *
   * Matched against the field's `autocomplete`, `name`, `id`, `placeholder`,
   * `aria-label` and its visible label text, all at once. Every ATS names these
   * differently — Greenhouse ships `job_application[first_name]`, Workday ships
   * a generated id and a label — so no single attribute is reliable and the
   * union of them is much harder to defeat than any one.
   *
   * Order matters. `first_name` must be tested before `name`, and `linkedin`
   * before `website`, because the looser pattern would otherwise swallow the
   * field the stricter one wanted.
   */
  /**
   * The two sponsorship phrasings, kept apart because forms ask both.
   *
   * "Will you require sponsorship?" and "Are you authorized to work without
   * sponsorship?" want opposite answers to the same fact, and a field that
   * matches both — "authorized to work without requiring sponsorship" is real
   * and common — is one where filling either answer has even odds of being a
   * false statement on an employer's form. Those are left blank; see
   * `valueFor`.
   */
  const SPONSORSHIP_RE = /(require|need|request).{0,30}sponsor|sponsor.{0,30}(require|need|now or in the future)/i;
  const AUTHORIZATION_RE = /(legally[\s_-]*authoriz|authoriz.{0,20}to work|work[\s_-]*authoriz|eligible to work|right to work)/i;

  const FIELD_RULES = [
    ["first_name", /(^|[^a-z])(first[\s_-]*name|given[\s_-]*name|fname)/i],
    ["last_name", /(^|[^a-z])(last[\s_-]*name|family[\s_-]*name|surname|lname)/i],
    ["email", /e-?mail/i],
    ["phone", /(phone|mobile|telephone|contact[\s_-]*number)/i],
    ["linkedin", /linked[\s_-]*in/i],
    ["github", /(github|git[\s_-]*hub)/i],
    ["website", /(website|portfolio|personal[\s_-]*site|blog)/i],
    ["school", /(school|university|college|institution)/i],
    ["degree", /degree/i],
    ["field_of_study", /(field[\s_-]*of[\s_-]*study|major|discipline)/i],
    ["location", /(city|location|address|where.*based)/i],
    // The screening questions, before the loose name rule below. Each is
    // answered once on the profile's Screening tab; blank there stays blank
    // here, because a guessed answer on a legal declaration is worse than an
    // empty box — the empty box gets noticed.
    ["sponsorship_required", SPONSORSHIP_RE],
    ["work_authorization", AUTHORIZATION_RE],
    ["start_date", /(start[\s_-]*date|when.*(can|could).*start|availability|notice[\s_-]*period|earliest.*(start|availability))/i],
    ["salary_expectation", /(salary|compensation|pay).*(expect|desired|require|range)|expected[\s_-]*(salary|compensation)|desired[\s_-]*(salary|compensation|pay)/i],
    ["referral_source", /(how did you hear|hear about us|referral[\s_-]*source|where did you (find|hear))/i],
    ["full_name", /(^|[^a-z])(full[\s_-]*name|your[\s_-]*name|name)/i],
  ];

  /** Everything a field is described by, as one lowercase haystack. */
  function describe(field) {
    const bits = [
      field.getAttribute("autocomplete"),
      field.name,
      field.id,
      field.getAttribute("placeholder"),
      field.getAttribute("aria-label"),
    ];

    // The visible label, which on Workday is the only thing that says anything.
    if (field.id) {
      // `CSS` here is the global, not this file's stylesheet — which is why
      // that constant is called PANEL_CSS. Shadowing it made `CSS.escape`
      // a property of a template string, so reading the label of any field
      // with an id threw and took the whole fill with it.
      const label = document.querySelector(`label[for="${CSS.escape(field.id)}"]`);
      if (label) bits.push(label.textContent);
    }
    const wrapping = field.closest("label");
    if (wrapping) bits.push(wrapping.textContent);

    return bits.filter(Boolean).join(" ").slice(0, 400).toLowerCase();
  }

  function valueFor(field, values) {
    const haystack = describe(field);
    if (!haystack) return null;
    // A question that is about sponsorship AND about authorization is asking
    // one of them inverted, and there is no way to tell which. Either answer
    // has even odds of being a false statement on an employer's form.
    if (SPONSORSHIP_RE.test(haystack) && AUTHORIZATION_RE.test(haystack)) {
      return null;
    }
    for (const [key, pattern] of FIELD_RULES) {
      if (pattern.test(haystack) && values[key]) return [key, values[key]];
    }
    return null;
  }

  /**
   * Set a value the way a framework will believe.
   *
   * React and Angular track the input's value internally and ignore a plain
   * assignment, so the field looks filled and submits empty — the worst
   * possible failure here. Writing through the native setter and then
   * dispatching the events a real keystroke produces is what makes the
   * framework accept it.
   */
  function setValue(field, value) {
    const proto =
      field instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(field, value);
    else field.value = value;

    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /**
   * Choose the option that matches a written answer. False when none does.
   *
   * Dropdowns are where the screening answers actually live — "Will you
   * require sponsorship?" is a `<select>` far more often than a text box — and
   * they are also where a wrong answer is most dangerous, because it looks
   * deliberate. So the matching is strict and refuses ties: exact text or
   * value, then a prefix ("Yes" against "Yes, I will require sponsorship"),
   * and nothing looser. Anything ambiguous is left for the user.
   */
  function chooseOption(select, value) {
    const want = normalize(value);
    if (!want) return false;

    const options = Array.from(select.options).filter((option, index) => {
      if (option.disabled) return false;
      // The placeholder row. Selecting it is the same as answering nothing,
      // and on a required field it is worse — it looks answered.
      if (index === 0 && !normalize(option.value)) return false;
      return Boolean(normalize(option.textContent) || normalize(option.value));
    });

    const tiers = [
      (option) =>
        normalize(option.textContent) === want || normalize(option.value) === want,
      (option) =>
        want.startsWith(normalize(option.textContent)) ||
        normalize(option.textContent).startsWith(want),
    ];
    for (const matches of tiers) {
      const hits = options.filter(matches);
      // Exactly one, or it is a guess. Two options starting with "yes" means
      // the form is distinguishing something this cannot see.
      if (hits.length === 1) {
        const setter = Object.getOwnPropertyDescriptor(
          HTMLSelectElement.prototype,
          "value",
        )?.set;
        if (setter) setter.call(select, hits[0].value);
        else select.value = hits[0].value;
        select.dispatchEvent(new Event("input", { bubbles: true }));
        select.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
    }
    return false;
  }

  function normalize(text) {
    return (text || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function fillableFields() {
    return Array.from(
      document.querySelectorAll("input, textarea, select"),
    ).filter((field) => {
      if (field.disabled || field.readOnly) return false;
      if (field.type && /hidden|password|file|submit|button|checkbox|radio/i.test(field.type)) {
        return false;
      }
      // Never overwrite something already answered. A half-completed form is
      // the common case, and clobbering an answer is worse than skipping it.
      // For a dropdown "already answered" means anything but the placeholder,
      // since a select always reports some value.
      if (field instanceof HTMLSelectElement) {
        if (field.selectedIndex > 0 && normalize(field.value)) return false;
      } else if (field.value && field.value.trim()) {
        return false;
      }
      const box = field.getBoundingClientRect();
      return box.width > 0 && box.height > 0;
    });
  }

  async function fillForm(box, button) {
    button.disabled = true;
    button.textContent = "Filling…";

    const reply = await ask("/api/agent/autofill-fields");
    if (reply.error) {
      button.disabled = false;
      button.textContent = "Fill this form";
      line(box, reply.error);
      return;
    }

    const values = reply.data || {};
    const filled = [];
    const skipped = [];
    for (const field of fillableFields()) {
      const match = valueFor(field, values);
      if (!match) continue;
      if (field instanceof HTMLSelectElement) {
        // A dropdown whose options do not clearly contain the answer is left
        // alone and reported. Picking the nearest option is how you end up
        // declaring the wrong work authorization.
        if (!chooseOption(field, match[1])) {
          skipped.push(match[0]);
          continue;
        }
      } else {
        setValue(field, match[1]);
      }
      // Marked rather than merely filled: you are about to submit this to an
      // employer, so what a machine wrote must be obvious at a glance.
      field.style.outline = "2px solid #2563eb";
      field.style.outlineOffset = "1px";
      filled.push(match[0]);
    }

    button.disabled = false;
    button.textContent = "Fill this form";
    line(
      box,
      filled.length
        ? `Filled ${filled.length}: ${filled.join(", ")}. Outlined in blue — check them, then submit yourself.`
        : "Nothing matched. Either the fields are already filled, or this form names them in a way I do not recognise.",
    );
    if (skipped.length) {
      line(
        box,
        `Left for you: ${skipped.join(", ")} — none of the dropdown options ` +
          "clearly matched your answer, and picking the nearest one is how a " +
          "form ends up declaring something you did not say.",
      );
    }
  }

  /** Whether this page looks like something worth offering to fill. */
  function looksLikeAForm() {
    return fillableFields().length >= 3;
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
