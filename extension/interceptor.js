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

  function maybeOffer(url, text) {
    if (!url || !text) return;
    if (text.length > MAX_BYTES) return;
    tally.json += 1;

    const named = INTERESTING.test(url);
    if (!named) tally.url_no += 1;

    // Cheap rejection before the expensive parse: a job payload names one of
    // these somewhere.
    if (named && SHAPE.test(text)) {
      try {
        offer(JSON.parse(text), url, false);
        tally.sent += 1;
      } catch (_) {
        // Not JSON. Common and uninteresting.
      }
      return;
    }
    tally.shape_no += 1;

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
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (_) {
      return;
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
      maybeOffer(xhr.__jobappUrl, xhr.responseText);
      return;
    }
    // Already parsed for us. This was skipped outright, which made every site
    // using axios — or anything else that sets `responseType = "json"` —
    // invisible to the harvest, because reading `responseText` on one of those
    // throws rather than returning the body.
    if (kind === "json" && xhr.response) {
      try {
        maybeOffer(xhr.__jobappUrl, JSON.stringify(xhr.response));
      } catch (_) {
        /* circular or too large to re-serialise */
      }
    }
  }

  // --- fetch -------------------------------------------------------------
  const nativeFetch = window.fetch;

  function inspect(response) {
    try {
      const type = response.headers.get("content-type") || "";
      if (type.includes("json")) {
        // A clone, so the page still gets to read its own body. Consuming the
        // original would break the site we are riding along on.
        response
          .clone()
          .text()
          .then((text) => maybeOffer(response.url, text))
          .catch(() => {});
      }
    } catch (_) {
      /* never let instrumentation break the page's own request */
    }
  }

  // Returns the native promise rather than awaiting it.
  //
  // An `async` wrapper made every fetch on the page pass through a frame of
  // ours, so a request the *page* made and the page's own CSP refused — an ad
  // tag, an analytics beacon — surfaced with `interceptor.js` in its stack.
  // Nothing was broken by that, but it is misleading to read, and a site
  // running Bugsnag or Datadog would have posted our filename to its own error
  // tracker for a failure that had nothing to do with us.
  //
  // Handing back the promise the native call produced avoids the extra frame
  // and the extra microtask. The inspection rides alongside on a derived
  // promise with its own rejection handler, so a refused request stays
  // entirely the page's business and cannot become an unhandled rejection of
  // ours.
  window.fetch = function (...args) {
    const response = nativeFetch.apply(this, args);
    try {
      response.then(inspect, () => {});
    } catch (_) {
      /* not a promise; hand back whatever it was untouched */
    }
    return response;
  };

  // --- XMLHttpRequest ----------------------------------------------------
  const nativeOpen = XMLHttpRequest.prototype.open;
  const nativeSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__jobappUrl = url;
    return nativeOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        offerXhrBody(this);
      } catch (_) {
        /* as above */
      }
    });
    return nativeSend.apply(this, args);
  };
})();
