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
  const INTERESTING = /(job|posting|search|hiring)/i;

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
    if (!INTERESTING.test(url)) return;
    if (text.length > MAX_BYTES) return;

    // Cheap rejection before the expensive parse: a job payload names one of
    // these somewhere.
    if (SHAPE.test(text)) {
      try {
        offer(JSON.parse(text), url, false);
      } catch (_) {
        // Not JSON. Common and uninteresting.
      }
      return;
    }

    // A near miss, kept as evidence rather than dropped.
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
    offer(parsed, url, true);
  }

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
  window.fetch = async function (...args) {
    const response = await nativeFetch.apply(this, args);
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
