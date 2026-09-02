/**
 * Reads the responses the page was already receiving. Runs in the MAIN world.
 *
 * The obvious way to get jobs off a page is to scrape the DOM. That is the
 * wrong layer: CSS classes are regenerated on every redesign, so selector-based
 * extraction rots on someone else's schedule, and it rots silently — a changed
 * class name yields zero jobs, which is indistinguishable from an empty page.
 *
 * The page's own API responses are far more stable. LinkedIn has reshaped its
 * job UI many times while `voyagerJobsDash*` kept returning the same fields,
 * because those fields are what their own client is built against. They also
 * carry more than the page renders — full descriptions, applicant counts,
 * salary — and reading them costs nothing: these are responses the browser
 * already fetched because you loaded the page.
 *
 * Running in MAIN is what makes this possible. A normal content script gets an
 * isolated copy of `window`, so patching `fetch` there would patch a `fetch`
 * the page never calls. MAIN shares the page's globals — and, because it does,
 * this file cannot use `chrome.*` at all. Findings go out by postMessage to the
 * isolated-world relay, which is the only side that can reach the extension.
 */

(() => {
  // The page may be injected into twice (bfcache, SPA remount). Patching a
  // patch would double every message.
  if (window.__jobappHarvestInstalled) return;
  window.__jobappHarvestInstalled = true;

  const CHANNEL = "jobapp-harvest";

  // Endpoints worth forwarding. Deliberately loose — matching the exact
  // Voyager route names would be the same brittleness as CSS selectors, one
  // layer down. The server decides what is job-shaped; this only avoids
  // shipping the whole internet at it.
  const INTERESTING =
    /(job|posting|search|hiring|career|vacanc|opening|listing|position|graphql)/i;

  // Responses larger than this are not job lists, they are asset manifests.
  const MAX_BYTES = 3_000_000;

  // Keys a job payload names somewhere. Widened to snake_case, which is what
  // every Python and Rails backend produces: `"job_title"` does not match a
  // pattern anchored on `"title"`, because the quote sits before `job`. A
  // board whose API answered in snake_case was invisible — the response
  // arrived, was read, matched nothing, and was dropped without trace.
  const SHAPE =
    /"(title|jobTitle|job_title|jobtitle|companyName|company_name|employer_name|jobPostingId|job_id|position|positionTitle)"/i;

  // Near misses: JSON on a job-shaped URL that names none of the keys above.
  //
  // These used to be discarded silently, and that is why a board could open,
  // scroll, paginate and yield nothing with no "Learn" button offered — the
  // button is built from stored samples, and a payload that never left the
  // browser cannot become one. Sending a few per page turns "nothing
  // forwarded" from a dead end into something the recipe learner can work on.
  //
  // Capped hard: this is diagnostic, and a page that answers in JSON for every
  // widget on it should not post forty of them.
  const MAX_PROBES = 4;
  const MAX_PROBE_BYTES = 400_000;
  let probes = 0;

  /**
   * Roughly "same company", for deciding what is worth probing.
   *
   * Last two labels, which is wrong for `.co.uk` and right for everything this
   * meets in practice. It only gates the probe — a payload that actually names
   * job fields is forwarded from anywhere — so being approximate costs a
   * diagnostic sample rather than a job.
   */
  function sameSite(url) {
    try {
      const there = new URL(url, location.href).hostname.split(".").slice(-2).join(".");
      const here = location.hostname.split(".").slice(-2).join(".");
      return Boolean(there) && there === here;
    } catch (_) {
      return false;
    }
  }

  // What this page's reader actually saw. Reported once per page, because
  // "nothing forwarded" was hiding two completely different situations: a page
  // whose responses we looked at and rejected, and a page where we saw no JSON
  // at all. The first is a filter to widen; the second means the listings are
  // not coming over an API we can read, and no amount of widening helps.
  //
  // Without this the panel could only say "nothing came back", which is where
  // every one of these investigations stalled.
  const tally = { json: 0, url_no: 0, shape_no: 0, sent: 0, probed: 0 };

  function offer(payload, sourceUrl, probe) {
    try {
      window.postMessage(
        {
          channel: CHANNEL,
          payload,
          sourceUrl: String(sourceUrl || location.href),
          probe: Boolean(probe),
        },
        location.origin,
      );
    } catch (_) {
      // Payload not structured-cloneable. Nothing to do and nothing worth
      // interrupting the page over.
    }
  }

  /** Whether a body is worth reading at all, without parsing it to find out. */
  function looksLikeJson(text) {
    if (typeof text !== "string") return false;
    const head = text.slice(0, 200).trimStart();
    return head.startsWith("{") || head.startsWith("[");
  }

  /**
   * Consider one response body.
   *
   * Takes the text, an already-parsed value, or both. The parsed form is the
   * common one now: we read bodies where the page parses them, so the object
   * already exists and re-parsing it would be work done twice.
   */
  function maybeOffer(url, text, parsed) {
    if (!url) return;
    if (text === undefined || text === null) {
      if (parsed === undefined) return;
      try {
        text = JSON.stringify(parsed);
      } catch (_) {
        return; // circular, or too large to re-serialise
      }
    }
    if (!text || text.length > MAX_BYTES) return;
    tally.json += 1;

    const named = INTERESTING.test(url);
    if (!named) tally.url_no += 1;

    // Cheap rejection before the expensive parse: a job payload names one of
    // these somewhere.
    if (named && SHAPE.test(text)) {
      try {
        offer(parsed !== undefined ? parsed : JSON.parse(text), url, false);
        tally.sent += 1;
      } catch (_) {
        // Not JSON. Common and uninteresting.
      }
      return;
    }
    // Only when the URL passed and the *shape* was what failed. The two
    // counters are meant to be different diagnoses — "our URL guess does not
    // name this board's API" and "this payload has no job in it" — and
    // counting a URL rejection in both made the second unreadable: every
    // telemetry response on the page landed in it too.
    if (named) tally.shape_no += 1;

    // A near miss, kept as evidence rather than dropped — and deliberately
    // *not* behind the URL filter above.
    //
    // It used to be, which defeated the whole point of it: the probe exists to
    // catch the payloads our guesses miss, so gating it behind the same guess
    // meant a board whose API lives at a URL the filter does not name produced
    // no forward, no probe, no sample, and no Learn button. Nothing at all,
    // which is exactly what several boards did. The cap and the structure test
    // are what keep this honest instead.
    if (probes >= MAX_PROBES || text.length > MAX_PROBE_BYTES) return;

    // Off the board's own domain, a probe has to earn it with a job-shaped
    // URL. Everything a modern job board loads is on somebody else's domain —
    // FullStory, Bugsnag, PostHog, StackAdapt, ZoomInfo, Cognito, Segment —
    // and all of it answers in structured JSON, so the probe budget went on
    // session tokens and feature flags. Thirteen of the fifteen hosts in the
    // sample store were telemetry, which is evidence about nothing crowding
    // out the payloads the recipe learner exists to read.
    if (!sameSite(url) && !named) return;

    if (parsed === undefined) {
      try {
        parsed = JSON.parse(text);
      } catch (_) {
        return;
      }
    }
    // Something with structure. A bare string, number or empty object is a
    // ping or a feature flag, and describes nothing worth learning from.
    const interesting =
      (Array.isArray(parsed) && parsed.length) ||
      (parsed && typeof parsed === "object" && Object.keys(parsed).length);
    if (!interesting) return;
    probes += 1;
    tally.probed += 1;
    offer(parsed, url, true);
  }

  // Reported a few seconds in, and again as the page goes away.
  //
  // Both, because neither alone is reliable here. A crawl closes the tab on
  // its own schedule and a message posted from `pagehide` may not survive it,
  // so waiting for the unload risks losing the report on exactly the
  // fast-closing pages this was written to explain. And the timer alone would
  // miss everything a lazily-loading board fetches after it.
  //
  // The second report carries the *difference*, not the running total. Sending
  // the total twice would have the server add a page's responses to itself and
  // report a site as busier than it is — which is the sort of thing that makes
  // a diagnostic panel worse than none. `first` marks the report that stands
  // for the page itself, so counting pages stays a count of pages.
  const reported = { json: 0, url_no: 0, shape_no: 0, sent: 0, probed: 0 };
  let everReported = false;

  function reportTally() {
    const delta = {};
    let any = false;
    for (const key of Object.keys(tally)) {
      delta[key] = tally[key] - reported[key];
      if (delta[key]) any = true;
    }
    // A page that saw nothing still reports, once — that zero is the finding.
    if (everReported && !any) return;
    for (const key of Object.keys(tally)) reported[key] = tally[key];
    delta.first = !everReported;
    everReported = true;
    try {
      window.postMessage(
        { channel: CHANNEL, stats: delta, sourceUrl: String(location.href) },
        location.origin,
      );
    } catch (_) {
      /* nothing worth interrupting the page over */
    }
  }

  setTimeout(reportTally, 3000);
  window.addEventListener("pagehide", reportTally);

  /** Hand a response body to the reader, whatever shape XHR returned it in. */
  function offerXhrBody(xhr) {
    const kind = xhr.responseType || "text";
    if (kind === "text" || kind === "") {
      // Same guard as the `text()` path: a page reading its own HTML this way
      // is common, and counting those would put a number in the panel's
      // "responses seen" that means nothing.
      if (looksLikeJson(xhr.responseText)) {
        maybeOffer(xhr.__jobappUrl, xhr.responseText);
      }
      return;
    }
    // Already parsed for us. This was skipped outright, which made every site
    // using axios — or anything else that sets `responseType = "json"` —
    // invisible to the harvest, because reading `responseText` on one of those
    // throws rather than returning the body.
    if (kind === "json" && xhr.response) {
      maybeOffer(xhr.__jobappUrl, undefined, xhr.response);
    }
  }

  // --- reading the body, not the request ---------------------------------
  //
  // `window.fetch` is deliberately left alone.
  //
  // Patching it worked, and it put this file on the stack of every request the
  // page made — including requests that had nothing to do with us and failed
  // for reasons of their own. Handshake refuses its own Google Ads beacon
  // under its own CSP, several times a page, and each refusal was reported
  // with `interceptor.js` named as the caller. Nothing was broken by that, but
  // it is alarming to read, and a site running Bugsnag or Datadog would have
  // filed our filename against a failure we had no part in.
  //
  // Two earlier attempts tried to shrink that frame — returning the native
  // promise instead of awaiting it, dropping the `async` wrapper. Neither
  // could work: CSP is enforced when the request is *initiated*, and if we are
  // `window.fetch` then we are the calling frame by definition.
  //
  // So the interception moves one step later, to where the page reads the body
  // it got back. `Response.prototype.json` is what a page calls when it has a
  // payload worth parsing, which is exactly the set we want and no more — a
  // request that is refused never produces a Response, so a blocked ad beacon
  // now happens entirely without us.
  //
  // It is also less work than what it replaces. The old path cloned every JSON
  // response and re-read the copy; this one reads the object the page was
  // going to parse anyway.
  //
  // What it gives up: a body the page fetches and never parses, and one read
  // through `response.body` as a stream. Neither describes a job list — a
  // board that fetches its listings and does not parse them has not rendered
  // them either.
  const nativeJson = Response.prototype.json;
  const nativeText = Response.prototype.text;

  Response.prototype.json = function (...args) {
    const result = nativeJson.apply(this, args);
    try {
      const url = this.url;
      // A derived promise with its own rejection handler. The page's promise is
      // handed back untouched, so nothing here can turn its failure into an
      // unhandled rejection of ours.
      result.then(
        (value) => {
          try {
            maybeOffer(url, undefined, value);
          } catch (_) {
            /* never let instrumentation break the page's own parse */
          }
        },
        () => {},
      );
    } catch (_) {
      /* not a promise; hand back whatever it was untouched */
    }
    return result;
  };

  Response.prototype.text = function (...args) {
    const result = nativeText.apply(this, args);
    try {
      const url = this.url;
      result.then(
        (body) => {
          try {
            // Only bodies that could be JSON. A page reading its own HTML
            // through `text()` is common, and counting those would put a
            // number in the panel's "responses seen" that means nothing.
            if (looksLikeJson(body)) maybeOffer(url, body);
          } catch (_) {
            /* as above */
          }
        },
        () => {},
      );
    } catch (_) {
      /* as above */
    }
    return result;
  };

  // --- XMLHttpRequest ----------------------------------------------------
  //
  // `send` is left alone for the same reason as `fetch`: that is where the
  // request is initiated, so that is the frame a CSP refusal would name. The
  // listener goes on in `open` instead, which has already returned by the time
  // anything is sent.
  const nativeOpen = XMLHttpRequest.prototype.open;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__jobappUrl = url;
    // `open` may be called more than once on a reused request object, and a
    // second listener would offer every body twice.
    if (!this.__jobappWatched) {
      this.__jobappWatched = true;
      try {
        this.addEventListener("load", () => {
          try {
            offerXhrBody(this);
          } catch (_) {
            /* never let instrumentation break the page's own request */
          }
        });
      } catch (_) {
        /* not a real XHR; leave it entirely alone */
      }
    }
    return nativeOpen.call(this, method, url, ...rest);
  };
})();
