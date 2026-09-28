/**
 * Filling application forms: the matching and typing, apart from the panel.
 *
 * A plain script rather than a module, injected before overlay.js into the
 * same (isolated) world, so the panel can call `JobAppAutofill` and a test can
 * load this file into a fixture page on its own. Nothing here talks to the
 * extension or the server; it is handed the values and reports what it did.
 *
 * What it answers, in order, for each question on the page:
 *
 *   1. The profile: name, contact, links, education, and the screening answers
 *      (work authorization, sponsorship, start date, salary, referral source).
 *   2. Voluntary self-identification — gender, race, veteran status,
 *      disability — only when the profile says to decline them, and only by
 *      choosing the form's own decline option.
 *   3. Remembered answers: a question the user answered by hand once, filled
 *      the same way next time it is asked in the same words. The idea is
 *      job_app_filler's (github.com/berellevy/job_app_filler) and autograph's,
 *      which both keep a store of answered questions; here it lives on the
 *      user's own server rather than in the browser.
 *
 * And the rules that make it safe to press on an employer's form:
 *
 *   - Never overwrite. A field with a value, a chosen option or a checked
 *     radio is the user's answer.
 *   - Never guess a choice. An option is chosen only when exactly one matches;
 *     anything else is left and reported.
 *   - Never submit, never tick a consent box, never touch a file or password.
 *   - Mark everything written, so it is read before it is sent.
 */

(() => {
  if (globalThis.JobAppAutofill) return;

  /**
   * The two sponsorship phrasings, kept apart because forms ask both.
   *
   * "Will you require sponsorship?" and "Are you authorized to work without
   * sponsorship?" want opposite answers to the same fact, and a field that
   * matches both — "authorized to work without requiring sponsorship" is real
   * and common — is one where filling either answer has even odds of being a
   * false statement on an employer's form. Those are left blank.
   */
  // `requir`, not `require`: "without requiring sponsorship" is the commonest
  // way the inverted question is put, and `require` does not match it.
  const SPONSORSHIP_RE = /(requir|need|request).{0,30}sponsor|sponsor.{0,30}(requir|need|now or in the future)/i;
  const AUTHORIZATION_RE = /(legally[\s_-]*authoriz|authoriz.{0,20}to work|work[\s_-]*authoriz|eligible to work|right to work)/i;

  // The equal-opportunity questions US forms ask as "voluntary".
  const EEO_RE = /\b(gender|sex|race|racial|ethnicity|ethnic|hispanic|latino|latina|latinx|veteran|disability|disabilities|disabled)\b/i;
  // An answer, or an option, that declines to say.
  const DECLINE_RE = /(decline|prefer not|rather not|choose not|do not wish|don'?t wish|not wish to|do not want|don'?t want|not to (answer|disclose|say|identify|self[\s-]*identify|provide))/i;

  /**
   * Which profile value belongs in a field, from how it is labelled.
   *
   * Order matters. `first_name` must be tested before `name`, and `linkedin`
   * before `website`, because the looser pattern would otherwise swallow the
   * field the stricter one wanted.
   */
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
    // Whole words: "ethnicity" contains "city", and a race question answered
    // with a home town is exactly the wrong kind of wrong.
    ["location", /(^|[^a-z])(city|location|address)([^a-z]|$)|where.*based/i],
    // The screening questions, before the loose name rule below. Blank on the
    // profile stays blank here: a guessed answer on a legal declaration is
    // worse than an empty box, because the empty box gets noticed.
    ["sponsorship_required", SPONSORSHIP_RE],
    ["work_authorization", AUTHORIZATION_RE],
    ["start_date", /(start[\s_-]*date|when.*(can|could).*start|availability|notice[\s_-]*period|earliest.*(start|availability))/i],
    ["salary_expectation", /(salary|compensation|pay).*(expect|desired|require|range)|expected[\s_-]*(salary|compensation)|desired[\s_-]*(salary|compensation|pay)/i],
    ["referral_source", /(how did you hear|hear about us|referral[\s_-]*source|where did you (find|hear))/i],
    ["full_name", /(^|[^a-z])(full[\s_-]*name|your[\s_-]*name|name)/i],
  ];

  // Never remembered and never filled from memory, whatever the form calls it.
  const SENSITIVE_RE = /(password|passcode|social security|\bssn\b|date of birth|birth ?date|\bdob\b|bank|routing|account number|credit card|card number|\bcvv\b|driver'?s licen[cs]e number|passport number)/i;

  // What this fill wrote, so a later fill or a harvest of answers can tell the
  // user's own typing from ours.
  const touched = new WeakSet();

  function normalize(text) {
    return (text || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  /** A question as a key: case, punctuation and "required" markers dropped. */
  function normalizeQuestion(text) {
    return normalize((text || "").replace(/\(required\)|\*/gi, " ")).slice(0, 300);
  }

  function textOf(node) {
    return node ? (node.textContent || "").replace(/\s+/g, " ").trim() : "";
  }

  function byIds(ids) {
    return (ids || "")
      .split(/\s+/)
      .map((id) => textOf(id && document.getElementById(id)))
      .filter(Boolean)
      .join(" ");
  }

  function labelFor(field) {
    if (!field.id) return "";
    // `CSS` is the global here; see overlay.js for the time it was shadowed.
    return textOf(document.querySelector(`label[for="${CSS.escape(field.id)}"]`));
  }

  /** The question a single field asks, as the page words it. */
  function questionText(field) {
    const text =
      byIds(field.getAttribute("aria-labelledby")) ||
      labelFor(field) ||
      field.getAttribute("aria-label") ||
      textOf(field.closest("label")) ||
      textOf(field.closest("fieldset")?.querySelector("legend")) ||
      field.getAttribute("placeholder") ||
      "";
    return text.replace(/\s+/g, " ").trim().slice(0, 300);
  }

  /** The question a radio group asks: its legend or group label, not an option's. */
  function groupQuestion(radio) {
    const group = radio.closest('[role="radiogroup"], [role="group"], fieldset');
    if (group) {
      const text =
        byIds(group.getAttribute("aria-labelledby")) ||
        group.getAttribute("aria-label") ||
        textOf(group.querySelector(":scope > legend")) ||
        textOf(group.querySelector("legend"));
      if (text) return text.slice(0, 300);
    }
    // No group markup: the nearest label-ish text above the first option.
    let node = radio.parentElement;
    for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
      const heading = node.querySelector("label:not(:has(input)), legend, h3, h4, p");
      if (heading && !heading.contains(radio)) return textOf(heading).slice(0, 300);
    }
    return radio.name || "";
  }

  /** Everything a field is described by, as one lowercase haystack. */
  function describe(field) {
    const bits = [
      field.getAttribute("autocomplete"),
      field.name,
      field.id,
      field.getAttribute("placeholder"),
      field.getAttribute("aria-label"),
      // Workday's own names for its fields, e.g. `legalNameSection_firstName`.
      field.getAttribute("data-automation-id"),
      byIds(field.getAttribute("aria-labelledby")),
      labelFor(field),
    ];
    const wrapping = field.closest("label");
    if (wrapping) bits.push(textOf(wrapping));
    return bits.filter(Boolean).join(" ").slice(0, 400).toLowerCase();
  }

  function visible(element) {
    const box = element.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }

  function mark(element) {
    touched.add(element);
    element.style.outline = "2px solid #2563eb";
    element.style.outlineOffset = "1px";
  }

  /**
   * Set a value the way a framework will believe.
   *
   * React and Angular track the input's value internally and ignore a plain
   * assignment, so the field looks filled and submits empty. Writing through
   * the native setter and dispatching the events a keystroke produces is what
   * makes the framework accept it; `blur` is for the forms that validate on
   * leaving a field (Workday among them).
   */
  function setValue(field, value) {
    const proto =
      field instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : field instanceof HTMLSelectElement
          ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(field, value);
    else field.value = value;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
    field.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  /**
   * The one item whose text matches a written answer, or null.
   *
   * Strict and refusing ties: exact text, then a prefix ("Yes" against "Yes,
   * I will require sponsorship"), and nothing looser. Two items starting
   * "yes" means the form is distinguishing something this cannot see. A
   * declining answer matches the form's one declining option, whatever it
   * calls it.
   */
  function pick(items, text, value) {
    if (DECLINE_RE.test(value || "")) {
      const declines = items.filter((item) => DECLINE_RE.test(text(item)));
      return declines.length === 1 ? declines[0] : null;
    }
    const want = normalize(value);
    if (!want) return null;
    const tiers = [
      (item) => normalize(text(item)) === want,
      (item) => {
        const have = normalize(text(item));
        return Boolean(have) && (want.startsWith(have) || have.startsWith(want));
      },
    ];
    for (const matches of tiers) {
      const hits = items.filter(matches);
      if (hits.length === 1) return hits[0];
      if (hits.length > 1) return null;
    }
    return null;
  }

  /** What to answer a question with: [key, value, how], or null. */
  function answerFor(haystack, question, values) {
    if (!haystack) return null;
    if (SPONSORSHIP_RE.test(haystack) && AUTHORIZATION_RE.test(haystack)) return null;
    if (SENSITIVE_RE.test(haystack)) return null;
    // Self-identification first: its words are specific, and a looser profile
    // rule must never get to answer one of these questions.
    const eeo = values.eeo_self_identification || "";
    if (EEO_RE.test(haystack)) {
      // Only a decline is applied across these: one answer cannot be right
      // for gender, race and disability at once.
      return DECLINE_RE.test(eeo) ? ["self_identification", eeo, "declined"] : null;
    }
    for (const [key, pattern] of FIELD_RULES) {
      if (pattern.test(haystack)) {
        return values[key] ? [key, values[key], "profile"] : null;
      }
    }
    const remembered = (values.remembered || {})[normalizeQuestion(question)];
    return remembered ? ["remembered", remembered, "remembered"] : null;
  }

  // -------------------------------------------------------------------------
  // The kinds of question a form asks
  // -------------------------------------------------------------------------

  function textFields() {
    return Array.from(document.querySelectorAll("input, textarea, select")).filter((field) => {
      if (field.disabled || field.readOnly) return false;
      if (field.type && /hidden|password|file|submit|button|checkbox|radio|reset|image/i.test(field.type)) {
        return false;
      }
      if (field instanceof HTMLSelectElement) {
        // A select always reports a value; "answered" is anything but the
        // placeholder row.
        if (field.selectedIndex > 0 && normalize(field.value)) return false;
      } else if (field.value && field.value.trim()) {
        return false;
      }
      return visible(field);
    });
  }

  function radioGroups() {
    const groups = new Map();
    for (const radio of document.querySelectorAll('input[type="radio"]')) {
      if (radio.disabled) continue;
      const key = radio.name || radio.closest('[role="radiogroup"], fieldset') || radio;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(radio);
    }
    return Array.from(groups.values()).filter(
      (radios) => radios.some(visible) && !radios.some((radio) => radio.checked),
    );
  }

  function radioLabel(radio) {
    return labelFor(radio) || textOf(radio.closest("label")) || radio.getAttribute("aria-label") || radio.value;
  }

  /** Custom dropdowns: a button that opens a listbox (Workday, Ashby, React-select). */
  function listboxButtons() {
    return Array.from(
      document.querySelectorAll('[aria-haspopup="listbox"]:not(input):not(select)'),
    ).filter((button) => {
      if (button.disabled || button.getAttribute("aria-disabled") === "true") return false;
      if (!visible(button)) return false;
      // Unanswered: still showing its prompt.
      const shown = normalize(textOf(button));
      return !shown || /^(select|select one|choose|choose one|please select|none selected)\b/.test(shown);
    });
  }

  function listboxQuestion(button) {
    return (
      byIds(button.getAttribute("aria-labelledby")).replace(textOf(button), "").trim() ||
      labelFor(button) ||
      button.getAttribute("aria-label") ||
      textOf(button.closest("fieldset")?.querySelector("legend")) ||
      ""
    ).slice(0, 300);
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function openOptions(button) {
    button.click();
    for (let tries = 0; tries < 20; tries += 1) {
      const owner = document.getElementById(button.getAttribute("aria-controls") || "");
      const scope = owner || document;
      const options = Array.from(scope.querySelectorAll('[role="option"]')).filter(visible);
      if (options.length) return options;
      await wait(50);
    }
    return [];
  }

  function closeOptions(button) {
    const escape = { key: "Escape", code: "Escape", keyCode: 27, bubbles: true };
    button.dispatchEvent(new KeyboardEvent("keydown", escape));
    document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", escape));
  }

  // -------------------------------------------------------------------------
  // Filling
  // -------------------------------------------------------------------------

  /**
   * Answer every question on the page it can. Resolves to a report:
   * `{filled: [key], skipped: [key], remembered: n, declined: n}`.
   */
  async function fill(values) {
    const report = { filled: [], skipped: [], remembered: 0, declined: 0 };
    const count = (answer) => {
      report.filled.push(answer[0]);
      if (answer[2] === "remembered") report.remembered += 1;
      if (answer[2] === "declined") report.declined += 1;
    };

    for (const field of textFields()) {
      const question = questionText(field);
      const answer = answerFor(describe(field), question, values);
      if (!answer) continue;
      if (field instanceof HTMLSelectElement) {
        const options = Array.from(field.options).filter(
          (option, index) => !option.disabled && !(index === 0 && !normalize(option.value)),
        );
        const option = pick(options, (o) => o.textContent || o.value, answer[1]);
        if (!option) {
          report.skipped.push(answer[0]);
          continue;
        }
        setValue(field, option.value);
      } else {
        setValue(field, answer[1]);
      }
      mark(field);
      count(answer);
    }

    for (const radios of radioGroups()) {
      const question = groupQuestion(radios[0]);
      const answer = answerFor(`${question} ${radios[0].name || ""}`.toLowerCase(), question, values);
      if (!answer) continue;
      const radio = pick(radios, radioLabel, answer[1]);
      if (!radio) {
        report.skipped.push(answer[0]);
        continue;
      }
      radio.click();
      mark(radio.closest("label") || radio);
      touched.add(radio);
      count(answer);
    }

    for (const button of listboxButtons()) {
      const question = listboxQuestion(button);
      const answer = answerFor(`${question} ${describe(button)}`.toLowerCase(), question, values);
      if (!answer) continue;
      const options = await openOptions(button);
      const option = pick(options, textOf, answer[1]);
      if (!option) {
        closeOptions(button);
        report.skipped.push(answer[0]);
        continue;
      }
      option.click();
      mark(button);
      count(answer);
      await wait(50);
    }
    return report;
  }

  /**
   * Keep filling as the form grows: Workday draws each step of an application
   * into the same page, and a fill pressed on step one never saw step three.
   * New fields are filled as they appear, for `minutes`, never overwriting.
   * Returns a function that stops watching.
   */
  function watch(values, onFill, minutes = 15) {
    let timer = null;
    let running = false;
    const observer = new MutationObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        if (running) return;
        running = true;
        try {
          const report = await fill(values);
          if (report.filled.length || report.skipped.length) onFill(report);
        } finally {
          running = false;
        }
      }, 400);
    });
    observer.observe(document.body, { childList: true, subtree: true });
    const stop = () => {
      clearTimeout(timer);
      observer.disconnect();
    };
    setTimeout(stop, minutes * 60 * 1000);
    return stop;
  }

  /**
   * The questions the user answered by hand, to remember: `[{question,
   * answer}]`. Not what this filled, not what the profile already covers, not
   * long-form text (a "why us" is about one company), and nothing sensitive.
   */
  function collectAnswers() {
    const found = [];
    const add = (question, haystack, answer) => {
      const text = (answer || "").trim();
      if (!question || !text || text.length > 500) return;
      if (SENSITIVE_RE.test(`${question} ${haystack}`)) return;
      if (FIELD_RULES.some(([, pattern]) => pattern.test(haystack))) return;
      if (EEO_RE.test(haystack)) return;
      found.push({ question, answer: text });
    };

    for (const field of document.querySelectorAll("input, select")) {
      if (touched.has(field) || field.disabled || !visible(field)) continue;
      if (field.type && /hidden|password|file|submit|button|checkbox|radio|reset|image/i.test(field.type)) {
        continue;
      }
      if (field instanceof HTMLSelectElement) {
        if (field.selectedIndex <= 0) continue;
        add(questionText(field), describe(field), textOf(field.options[field.selectedIndex]));
      } else {
        add(questionText(field), describe(field), field.value);
      }
    }
    for (const radios of radioGroupsAnswered()) {
      const chosen = radios.find((radio) => radio.checked);
      if (touched.has(chosen)) continue;
      const question = groupQuestion(radios[0]);
      add(question, `${question} ${radios[0].name || ""}`.toLowerCase(), radioLabel(chosen));
    }
    // One answer per question, the last one on the page winning.
    const unique = new Map(found.map((entry) => [normalizeQuestion(entry.question), entry]));
    return Array.from(unique.values());
  }

  function radioGroupsAnswered() {
    const groups = new Map();
    for (const radio of document.querySelectorAll('input[type="radio"]')) {
      const key = radio.name || radio.closest('[role="radiogroup"], fieldset') || radio;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(radio);
    }
    return Array.from(groups.values()).filter((radios) => radios.some((radio) => radio.checked));
  }

  function looksLikeAForm() {
    return textFields().length + radioGroups().length + listboxButtons().length >= 3;
  }

  /**
   * The long questions still waiting for an answer — "Why do you want to work
   * here?" — as `[{question, field, maxChars}]`, for the panel to offer a
   * draft of. Empty, visible text areas that ask something; never one a
   * profile rule answers, never self-identification, never anything
   * sensitive (the server refuses declarations as well).
   */
  function longQuestions() {
    const found = [];
    for (const field of document.querySelectorAll("textarea")) {
      if (field.disabled || field.readOnly || !visible(field)) continue;
      if (field.value && field.value.trim()) continue;
      const question = questionText(field);
      if (!question || question.length < 8) continue;
      const haystack = `${question} ${describe(field)}`.toLowerCase();
      if (SENSITIVE_RE.test(haystack) || EEO_RE.test(haystack)) continue;
      if (FIELD_RULES.some(([, pattern]) => pattern.test(haystack))) continue;
      found.push({ question, field, maxChars: field.maxLength > 0 ? field.maxLength : null });
    }
    return found;
  }

  /**
   * Put text the user accepted into a field: typed the way a framework will
   * believe, and outlined like everything else this fills. Refuses a field
   * that has text in it now — the user's own typing is never overwritten.
   */
  function put(field, text) {
    if (field.value && field.value.trim()) return false;
    setValue(field, text);
    mark(field);
    return true;
  }

  globalThis.JobAppAutofill = {
    fill,
    watch,
    collectAnswers,
    describe,
    looksLikeAForm,
    longQuestions,
    normalizeQuestion,
    put,
  };
})();
