/**
 * Asks Tsenta's own API for the pages its UI will not show. MAIN world.
 *
 * Every other site in this extension is read passively: you open a page, the
 * page fetches its listings, and `interceptor.js` reads the responses on the
 * way past. That works because the page eventually fetches everything it
 * displays. Tsenta is the case where it does not.
 *
 * Its board renders about five results until its search is submitted, then
 * loads more only as you scroll, twenty at a time — so the passive reader's
 * yield is bounded by how long a tab is willing to sit there scrolling, which
 * on a crawl is about a minute. Worse, its own client hardcodes
 * `autoApplyOnly: true`, so the postings it can auto-apply to are the only
 * ones the UI ever renders. Those it cannot are fetched by nobody, displayed
 * to nobody, and therefore read by nobody. Scrolling harder cannot reach them.
 *
 * So this asks directly. The board is served by `api.autojobs.me`, its client
 * authenticates with a Firebase ID token, and — usefully — that client already
 * publishes the token to browser extensions: it listens for a
 * `TSENTA_EXT_REQUEST_AUTH` message and answers with `TSENTA_AUTH_SYNC`
 * carrying the current token. That handshake is theirs, not ours; this uses it
 * as offered.
 *
 * Three things follow from running in the page's world rather than the
 * extension's, and all three are the reason it does:
 *
 * 1. **The request comes from their own origin**, so it passes the API's CORS
 *    check for exactly the same reason their client's requests do. From a
 *    service worker the origin would be `chrome-extension://…` and every
 *    request would be refused before it was sent.
 * 2. **The token never leaves the browser.** It is asked for, used, and
 *    dropped when the tab closes. Nothing is stored, and the server is never
 *    told it exists.
 * 3. **No new permission.** The page is already allowed to talk to its own
 *    API; the extension needs no host access to `autojobs.me` at all.
 *
 * Payloads go out on the same channel as everything else, through `relay.js`,
 * and land in the same reader on the server — `autojobs.me` is mapped to the
 * Tsenta source there, so a page fetched here is filed exactly as a page read
 * passively would be.
 */

(() => {
  // Registered per site and injected at document_start, so a bfcache restore
  // or an SPA remount can run this file twice in one page.
  if (window.__jobappTsentaInstalled) return;
  window.__jobappTsentaInstalled = true;

  const CHANNEL = "jobapp-harvest";

  // Their handshake, named by their own bundle. If they rename or remove it,
  // the ask below times out and this reports that it did — it does not fall
  // back to scraping a token out of storage, which would be reaching into
  // internals nobody offered.
  const REQUEST_AUTH = "TSENTA_EXT_REQUEST_AUTH";
  const AUTH_SYNC = "TSENTA_AUTH_SYNC";

  const API = "https://api.autojobs.me/api/v1/jobs/recommendations";

  // The query their client sends, with two filters left off.
  //
  // Both omissions are reading their own query builder rather than guessing.
  // It appends `autoApplyOnly` only when the flag is exactly `true`, and
  // appends `datePosted` only when it is set to something other than `all` —
  // so leaving each one out is how their own client expresses "no filter",
  // and there is no risk of the API reading a `false` as merely present.
  //
  // `autoApplyOnly` is the one that matters: their feed pins it on, which is
  // exactly what hides the postings this file exists to reach. Dropping
  // `datePosted` widens the window from a month to everything they hold.
  //
  // The location filter stays, because it is a filter on what is wanted
  // rather than on what is visible.
  const LOCATIONS = "country:US";

  // Their client asks for 20. A larger page is fewer round trips for the same
  // jobs, but there is no documentation saying whether it is honoured, so it
  // is tried once and abandoned on the first sign that it is not.
  const FIRST_LIMIT = 100;
  const FALLBACK_LIMIT = 20;

  // Budgets. The tab a crawl opens is closed on the crawl's schedule, not
  // ours, so this has to finish inside roughly a minute of work: forty pages
  // at a beat under a second each, and a row cap in case a page size is
  // ignored and every request returns the whole board.
  const MAX_PAGES = 40;
  const MAX_ROWS = 4000;
  const PAUSE_MS = 600;
  const MAX_BYTES = 3_000_000;

  // How long to wait for the app to answer with a token. Their listener only
  // replies once Firebase has resolved the signed-in user, which is a network
  // round trip after the page loads — so the ask is repeated rather than made
  // once and given up on.
  const AUTH_TIMEOUT_MS = 25000;
  const AUTH_RETRY_MS = 2000;

  // At most one sweep per quarter hour across every tab. Without this, opening
  // four Tsenta tabs would page the whole board four times; with it, a crawl
  // that visits every few hours always sweeps and a person clicking around
  // does not. Kept in the page's own storage because this world has no
  // `chrome.*` — one key, and a failure to write it costs a throttle, not a
  // sweep.
  const THROTTLE_KEY = "jobapp:tsenta:swept";
  const THROTTLE_MS = 15 * 60 * 1000;

  function post(message) {
    try {
      window.postMessage({ channel: CHANNEL, ...message }, location.origin);
    } catch (_) {
      /* nothing here is worth interrupting the page over */
    }
  }

  /** Hand one API response to the pipeline the passive reader also feeds. */
  function offer(payload, sourceUrl) {
    post({ payload, sourceUrl: String(sourceUrl), probe: false });
  }

  /**
   * Say what the sweep did, including when it did nothing.
   *
   * This is the whole reason there is a report at all. A sweep that never got
   * a token, one refused by the API, and one that ran and found the board
   * empty are three different problems with three different fixes, and all
   * three look identical from the server — no payloads arrived. Reporting the
   * absence without its cause is what made every earlier investigation in this
   * subsystem start from a DevTools capture.
   */
  function report(summary, ok) {
    post({ sweep: { ok: ok !== false, summary }, sourceUrl: String(location.href) });
  }

  function recentlySwept() {
    try {
      const last = Number(localStorage.getItem(THROTTLE_KEY) || 0);
      return Number.isFinite(last) && Date.now() - last < THROTTLE_MS;
    } catch (_) {
      return false;
    }
  }

  function markSwept() {
    try {
      localStorage.setItem(THROTTLE_KEY, String(Date.now()));
    } catch (_) {
      /* private mode, or storage full. One extra sweep is the whole cost. */
    }
  }

  /**
   * Ask the page's app for the current ID token.
   *
   * Resolves with the token, or "" if the app never answered — which is the
   * normal outcome on a marketing page, on a signed-out session, and on the
   * day they remove the handshake. All three want the same thing from us:
   * stop, and say so.
   */
  function askForToken() {
    return new Promise((resolve) => {
      let settled = false;

      function finish(token) {
        if (settled) return;
        settled = true;
        window.removeEventListener("message", onMessage);
        clearInterval(asker);
        clearTimeout(timer);
        resolve(token);
      }

      function onMessage(event) {
        // Only the page talking to itself. Their own bridge posts to
        // `window.location.origin` and checks the same thing on the way in.
        if (event.source !== window) return;
        const data = event.data;
        if (!data || data.type !== AUTH_SYNC) return;
        if (typeof data.idToken === "string" && data.idToken) finish(data.idToken);
      }

      function ask() {
        try {
          window.postMessage({ type: REQUEST_AUTH }, location.origin);
        } catch (_) {
          /* as above */
        }
      }

      window.addEventListener("message", onMessage);
      const asker = setInterval(ask, AUTH_RETRY_MS);
      const timer = setTimeout(() => finish(""), AUTH_TIMEOUT_MS);
      ask();
    });
  }

  function pageUrl(page, limit) {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    params.set("page", String(page));
    params.set("locations", LOCATIONS);
    return `${API}?${params.toString()}`;
  }

  /**
   * Fetch one page and parse it.
   *
   * Read through `arrayBuffer`, and that is deliberate: `interceptor.js`
   * patches `Response.prototype.json` and `.text`, so reading the body either
   * of those ways would have the same payload forwarded twice — once by this
   * file and once by the reader watching every response on the page.
   * `arrayBuffer` is untouched, which keeps the two paths from colliding
   * without either one needing to know about the other.
   */
  async function readPage(url, token) {
    let res;
    try {
      res = await fetch(url, {
        credentials: "include",
        headers: { accept: "application/json", authorization: `Bearer ${token}` },
      });
    } catch (error) {
      // A refused CORS preflight, an offline tab, a closing window. The
      // message is what tells them apart when this reaches the panel.
      return { status: 0, error: String((error && error.message) || error), body: null };
    }
    if (!res.ok) return { status: res.status, error: "", body: null };

    let buffer;
    try {
      buffer = await res.arrayBuffer();
    } catch (_) {
      return { status: res.status, error: "body unreadable", body: null };
    }
    if (buffer.byteLength > MAX_BYTES) {
      return { status: res.status, error: "body too large", body: null };
    }
    try {
      const text = new TextDecoder().decode(buffer);
      return { status: res.status, error: "", body: JSON.parse(text) };
    } catch (_) {
      return { status: res.status, error: "not json", body: null };
    }
  }

  /**
   * Roughly how many records a payload carries.
   *
   * Shape-based on purpose, exactly like the reader on the server: naming the
   * key their API happens to put the list under would be one more thing to
   * break when they rename it. The largest array of objects anywhere in the
   * response is the list, and that has been true of every job API this project
   * has met.
   *
   * It is only ever used to decide whether to ask for another page, so being
   * approximate costs at most one extra request.
   */
  function countRows(value, depth) {
    depth = depth || 0;
    if (depth > 6 || !value || typeof value !== "object") return 0;
    if (Array.isArray(value)) {
      let best = value.filter((item) => item && typeof item === "object").length;
      // Only a few children: this is a size estimate, not a full walk of a
      // payload that may be megabytes.
      for (const item of value.slice(0, 5)) {
        best = Math.max(best, countRows(item, depth + 1));
      }
      return best;
    }
    let best = 0;
    for (const key of Object.keys(value)) {
      best = Math.max(best, countRows(value[key], depth + 1));
    }
    return best;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function sweep() {
    if (recentlySwept()) return;

    // Claimed before the ask, not after it. Waiting for a token takes up to 25
    // seconds, and the check above is the only thing keeping two tabs from
    // sweeping the whole board at once — leaving the claim until afterwards
    // left a 25-second window in which both tabs passed it. Marking first
    // costs nothing that the old placement did not already cost: the mark was
    // already made whichever way the ask went, including for a page that never
    // answered.
    markSwept();

    const token = await askForToken();

    if (!token) {
      // A marketing page answering nothing is correct behaviour, and this
      // still reports it: on the board itself, the app not answering is the
      // entire explanation for an empty harvest, and the report names the page
      // it happened on.
      report({ stopped: "no token", pages: 0, rows: 0 }, false);
      return;
    }

    let limit = FIRST_LIMIT;
    let pages = 0;
    let rows = 0;
    let stopped = "end of list";
    let status = 0;
    let detail = "";

    // The page size actually served, which is not always the one asked for.
    //
    // Measured rather than assumed, because an API that quietly caps `limit`
    // at its own maximum answers a request for 100 with 200 OK and 20 rows —
    // and comparing that against what we asked for reads as "the list ended",
    // stopping the sweep on page one with twenty jobs. Comparing each page
    // against the size the *first* page came back at cannot make that mistake,
    // whether the cap is honoured, ignored, or silently lowered.
    let served = 0;

    for (let page = 1; page <= MAX_PAGES; page += 1) {
      let url = pageUrl(page, limit);
      let result = await readPage(url, token);

      // A page size they do not accept looks like a rejected request, so the
      // first one is retried at theirs before concluding anything. Only on
      // page one: past that, a refusal is a refusal.
      if (page === 1 && !result.body && limit !== FALLBACK_LIMIT) {
        limit = FALLBACK_LIMIT;
        await sleep(PAUSE_MS);
        // Reassigned, not shadowed: the body about to be forwarded came from
        // the retry, and offering it under the address of the request that was
        // refused files the rows against a URL that produced nothing.
        url = pageUrl(page, limit);
        result = await readPage(url, token);
      }

      if (!result.body) {
        status = result.status;
        detail = result.error;
        if (result.status === 401 || result.status === 403) {
          stopped = "not signed in";
        } else if (result.status >= 400) {
          stopped = `HTTP ${result.status}`;
        } else if (result.error) {
          // A 200 whose body we could not use — too large, or not JSON. The
          // status code is the least informative thing about it, and reporting
          // "HTTP 200" as the reason a sweep stopped reads as a bug in us.
          stopped = result.status ? result.error : `request failed: ${result.error}`;
        } else {
          stopped = "unreadable answer";
        }
        break;
      }

      const found = countRows(result.body);
      if (!found) {
        stopped = "empty page";
        break;
      }

      offer(result.body, url);
      pages += 1;
      rows += found;
      if (!served) served = found;

      // A short page is the last page. Checked after forwarding, so the tail
      // of the board is kept rather than thrown away for being small — and
      // against the size the board actually serves rather than the one asked
      // for, so a silently capped `limit` does not read as the end of the list.
      if (found < served) {
        stopped = "short page";
        break;
      }
      if (rows >= MAX_ROWS) {
        stopped = "row budget";
        break;
      }
      if (page === MAX_PAGES) stopped = "page budget";

      await sleep(PAUSE_MS);
    }

    // `served`, not `limit`: the number worth keeping is the one the board
    // answered with, which is the only record of whether asking for a bigger
    // page than its own client uses was honoured.
    report(
      { pages, rows, limit: served || limit, stopped, status, detail },
      pages > 0,
    );
  }

  sweep();
})();
