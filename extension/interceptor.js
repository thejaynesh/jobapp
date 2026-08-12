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

  function offer(payload, sourceUrl) {
    try {
      window.postMessage(
        { channel: CHANNEL, payload, sourceUrl: String(sourceUrl || location.href) },
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
    // Cheap rejection before the expensive parse: a job payload always names
    // one of these somewhere.
    if (!/"(title|jobTitle|companyName|jobPostingId)"/.test(text)) return;
    try {
      offer(JSON.parse(text), url);
    } catch (_) {
      // Not JSON. Common and uninteresting.
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
        if (this.responseType && this.responseType !== "text") return;
        maybeOffer(this.__jobappUrl, this.responseText);
      } catch (_) {
        /* as above */
      }
    });
    return nativeSend.apply(this, args);
  };
})();
