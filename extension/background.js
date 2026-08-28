/**
 * The agent loop: ask the server for work, run it, post the result back.
 *
 * The server never calls us. It cannot — this browser has no address, and it is
 * closed half the day. So the flow is entirely pull-based, and being offline is
 * not an error state, it is just a period where no work gets leased.
 *
 * Two facts about MV3 service workers shape everything here:
 *
 *   1. This worker is terminated after roughly 30 seconds idle. It is not a
 *      daemon. Anything remembered between wakeups lives in chrome.storage, and
 *      the poll ceiling stays under that timeout so a request never outlives
 *      the worker that is waiting on it. An in-flight fetch counts as activity,
 *      which is why a 25-second long poll survives and a 45-second one dies.
 *
 *   2. chrome.alarms is the only way to be woken reliably, and its floor is one
 *      minute. So the worst case for an idle queue is: work is enqueued, and up
 *      to a minute passes before anyone asks for it. Draining is much faster
 *      than that — after a task completes we poll again immediately rather than
 *      waiting for the next alarm, so a burst of ten tasks does not take ten
 *      minutes.
 */

// The sites harvesting can read. Shared with the options page rather than
// written out in both — see sites.js for how to add one.
import { HARVEST_SITES, siteForUrl } from "./sites.js";

const ALARM_NAME = "jobapp-poll";
const DEFAULTS = {
  serverUrl: "",
  token: "",
  agentId: "",
  enabled: false,
};

// ---------------------------------------------------------------------------
// Configuration and status
// ---------------------------------------------------------------------------

async function getConfig() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

/**
 * Status is written to storage rather than kept in memory, because this worker
 * is disposable — the options page needs to be able to show what happened on a
 * poll that ran in a worker which no longer exists.
 */
async function setStatus(patch) {
  const current = (await chrome.storage.local.get("status")).status || {};
  await chrome.storage.local.set({
    status: { ...current, ...patch, updatedAt: new Date().toISOString() },
  });
}

/** A stable-enough name for this install, so the server can tell engines apart. */
async function agentId() {
  const config = await getConfig();
  if (config.agentId) return config.agentId;
  const generated = `extension-${crypto.randomUUID().slice(0, 8)}`;
  await chrome.storage.local.set({ agentId: generated });
  return generated;
}

// ---------------------------------------------------------------------------
// Talking to the server
// ---------------------------------------------------------------------------

async function api(path, body, { timeoutMs = 40000 } = {}) {
  const { serverUrl, token } = await getConfig();
  if (!serverUrl || !token) throw new Error("Not configured yet.");

  // An abort guard, because a hung request would otherwise keep this worker
  // alive doing nothing until the browser loses patience with it.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(new URL(path, serverUrl).toString(), {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });

    // Read the server's own explanation where there is one. These messages are
    // written to be shown to a person — "AGENT_TOKEN is not set" is a fixable
    // instruction in a way that "HTTP 503" is not.
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (_) {
        /* not JSON; the status line is all we have */
      }
      throw new Error(detail);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Opening a page for real
// ---------------------------------------------------------------------------

/**
 * Load a URL in an actual browser window, when fetching it is refused.
 *
 * A `fetch` from a service worker sends no Referer, runs no JavaScript, paints
 * nothing and follows no meta-refresh. Aggregators screen for exactly that
 * shape, which is why Jooble and Indeed answer 403 here while the same URL
 * opens fine in a tab. A real navigation is not an imitation of a browser
 * visit; it is one.
 *
 * The window is created minimized and closed again immediately. That is still
 * more intrusive than a fetch, so this is an escalation rather than the default
 * path — cheap and silent first, visible only when that fails.
 */
const TAB_LOAD_TIMEOUT_MS = 25000;
// Time after "complete" for a meta-refresh or JS redirect to happen. The
// interstitials this exists for very often bounce a moment after painting.
const TAB_SETTLE_MS = 1800;
// How long to leave a "confirm you're human" window up before concluding
// nobody is at the machine. Long enough to notice and click, short enough that
// an unattended run loses one page rather than its whole evening — and it is
// only ever paid once per host per session.
const CHALLENGE_WAIT_MS = 90000;

// One at a time. Escalating a backlog of link resolutions in parallel would
// open a dozen windows at once, which is not a thing to do to someone's screen.
let tabQueue = Promise.resolve();

function withTabLock(fn) {
  const run = tabQueue.then(fn, fn);
  // Keep the chain alive whichever way this settles.
  tabQueue = run.then(() => {}, () => {});
  return run;
}

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") finish();
    };
    // A page that never finishes must not hold the queue open; whatever has
    // loaded by then is usually enough to read a redirect target out of.
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function clampSeconds(value, fallback, low, high) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return fallback;
  return Math.max(low, Math.min(high, Math.round(seconds)));
}

/**
 * Open a page and leave it open long enough to be read.
 *
 * Separate from `openInTab` because it wants the opposite things. That one
 * escalates a failed fetch and cares only where the URL landed, so it settles
 * briefly and returns the markup. This one does not want the markup at all —
 * the interceptor is already reading the page's API responses — it wants the
 * page to have time to *make* those requests, and on a job board the ones
 * carrying the posting body come after `load`.
 *
 * The scroll is part of that. A search page renders the first screen of cards
 * and fetches the rest when you move, so a tab that opens and sits still
 * harvests a fraction of what the page would have shown a reader.
 */
// The ceiling on one page's scrolling, whatever it was asked for. An MV3
// service worker is terminated when it looks idle, and an injected script that
// runs for minutes is exactly what that looks like from outside — so the
// budget stays well inside it, and a board with more to give simply gets
// visited again rather than held open.
const SCROLL_BUDGET_MS = 75000;

async function visitInTab(url, settleMs, passes, pauseMs, maxPages,
                          clickSelector) {
  return withTabLock(async () => {
    let win;
    try {
      win = await chrome.windows.create({ url, focused: false, state: "minimized" });
    } catch (error) {
      throw new Error(`could not open a window: ${error.message}`);
    }

    const tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
    if (!tabId) {
      await chrome.windows.remove(win.id).catch(() => {});
      throw new Error("the window opened without a tab.");
    }

    try {
      await waitForLoad(tabId, TAB_LOAD_TIMEOUT_MS);
      // Half the settle before scrolling, half after: the first lets the
      // posting body land, the second lets whatever the scroll asked for come
      // back before the tab is taken away.
      await new Promise((r) => setTimeout(r, Math.round(settleMs / 2)));

      // Before the scroll, not after: scrolling an interstitial finds nothing
      // and reports a shallow crawl, which reads as a broken scroll rather
      // than a page that was never reached.
      // Shorter here than on the resolve path: this runs on every page of a
      // sixty-page crawl, and a board's search results are substantial enough
      // that `awaitChallenge` returns on the first look.
      let challenge = "";
      const gate = await awaitChallenge(tabId, 4000);
      if (gate && gate.challenge) {
        challenge = await waitForHuman(win, tabId, url);
        if (challenge === "passed") {
          // The real page is only arriving now, so it gets the settle the
          // first one was given.
          await waitForLoad(tabId, TAB_LOAD_TIMEOUT_MS).catch(() => {});
          await new Promise((r) => setTimeout(r, Math.round(settleMs / 2)));
        }
      }

      let signed_in = true;
      let title = "";
      // How far down the list the scroll actually got. On an infinitely
      // scrolling board this is the only measure of whether the visit went
      // deep or gave up on the first stall.
      let scrolled = 0;
      // How many times new content arrived, and which element was scrolled.
      // The batch count is the honest measure of whether the scroll worked;
      // pixels can move on a page that loads nothing.
      let batches = 0;
      let scrollTargetSeen = "";
      // Whether the board asked us to slow down, and how deep the scroll got
      // before it did. Both travel back to the server: the first so it can
      // wait, the second so the next visit asks for a depth this board has
      // actually tolerated.
      let rate_limited = false;
      let passes_done = 0;
      // Result pages reached by clicking through. One on a board that does not
      // paginate — and one on a board that does is the signal that the control
      // was not found, which is the only way to tell that apart from a board
      // that genuinely has a single page.
      let pages_done = 1;
      // What the page offered by way of pagination, gathered only when the
      // visit got nowhere. The server turns this into a crawl recipe.
      let navigation = null;
      try {
        const [injected] = await chrome.scripting.executeScript({
          target: { tabId },
          args: [passes, SCROLL_BUDGET_MS, pauseMs, maxPages, clickSelector],
          func: async (maxPasses, budgetMs, pauseMs, maxPages, learnedSelector) => {
            // For an infinitely scrolling board this loop *is* the pagination.
            // There is no page-two URL to queue, so the only way to reach the
            // hundredth result is to keep asking — and each batch it pulls in
            // is another API response the interceptor reads on the way past.
            //
            // Two things here were wrong in the first version, and both made a
            // deep crawl come back with one batch and close in three seconds.

            // 1. It scrolled the *window*. These apps routinely put results in
            //    an inner overflow container inside a fixed-height layout, and
            //    then the window has nothing to scroll: scrollTo does nothing,
            //    no fetch is triggered, and the loop concludes the list ended.
            //    So find the element that actually scrolls, and re-find it each
            //    pass because the layout moves as content arrives.
            function scrollTarget() {
              const doc = document.scrollingElement || document.documentElement;
              if (doc && doc.scrollHeight > doc.clientHeight + 200) return doc;
              let best = doc;
              let bestArea = 0;
              for (const el of document.querySelectorAll("div,main,section,ul,ol")) {
                if (el.scrollHeight <= el.clientHeight + 200) continue;
                const style = getComputedStyle(el);
                if (!/(auto|scroll)/.test(style.overflowY)) continue;
                const area = el.clientHeight * el.clientWidth;
                if (area > bestArea) { bestArea = area; best = el; }
              }
              return best;
            }

            // 2. It measured progress by document height. A virtualized list
            //    recycles its rows, so the height never changes however much
            //    you load — which reads as "stopped growing" on the first pass.
            //    Counting nodes and links catches a batch rendering whether or
            //    not the page got taller.
            const signal = () =>
              document.getElementsByTagName("*").length +
              document.querySelectorAll("a[href]").length;

            // 3. It kept scrolling after the board had started saying no.
            //    An infinite list rate-limits: Greenhouse's asks you to wait a
            //    few minutes. Every further pass past that point is a request
            //    into a closed door, and requests into a closed door are how a
            //    few minutes becomes an hour. Stopping on the first sight of
            //    it is both politer and faster.
            const limited = () => {
              const text = (document.body ? document.body.innerText : "")
                .slice(0, 4000);
              return /too many requests|rate limit|slow down|try again in a (few|couple)|please wait a few|temporarily (blocked|unavailable)|you('| a)?re going too fast/i
                .test(text);
            };

            // 4. Some boards do not scroll at all. Hiring Cafe paginates with
            //    numbered buttons at the bottom, and scrolling a paginated
            //    board is a no-op — it reaches the bottom of page one and
            //    stops, so every visit harvested the first page and nothing
            //    else, forever, while reporting a perfectly healthy scroll.
            //
            //    Clicking the control is the only way through, and it has to
            //    be a click rather than a URL: these are single-page apps, and
            //    page two is often the same address.

            // What the list currently holds. Node counts cannot see a page
            // *change* — twenty rows replaced by twenty rows is the same
            // count — so pagination needs identity rather than size.
            const fingerprint = () =>
              Array.from(document.querySelectorAll("a[href]"))
                .slice(0, 40)
                .map((a) => a.getAttribute("href"))
                .join("|");

            // The next-page control, if this page has one that is still live.
            // `rel="next"` first because it is the one unambiguous signal;
            // everything after it is a guess at somebody's markup, kept narrow
            // by requiring the text to be short — "Next" is a button label,
            // whereas an element whose text merely contains "next page" is
            // usually a container holding one.
            const nextControl = () => {
              // A selector the server learned for this board wins, because it
              // was written against this page rather than against boards in
              // general. Everything below it is the guess that applies when
              // nothing has been learned.
              if (learnedSelector) {
                try {
                  const taught = document.querySelector(learnedSelector);
                  if (taught) return taught;
                } catch (_) {
                  // Not a selector this browser accepts. Fall through rather
                  // than fail the visit — the heuristics still work.
                }
              }

              const rel = document.querySelector("a[rel='next'], link[rel='next']");
              if (rel && rel.tagName === "A") return rel;

              const usable = (el) => {
                if (el.disabled) return false;
                if (el.getAttribute("aria-disabled") === "true") return false;
                const style = getComputedStyle(el);
                return style.display !== "none" && style.visibility !== "hidden";
              };
              const candidates = document.querySelectorAll(
                "button, a, [role='button']",
              );
              for (const el of candidates) {
                if (!usable(el)) continue;
                const label = (
                  el.getAttribute("aria-label") ||
                  el.getAttribute("title") ||
                  el.textContent ||
                  ""
                ).trim();
                if (!label || label.length > 24) continue;
                if (/^(next|next page|next \u203a|\u203a|\u00bb|\u2192|>)$/i.test(label)) {
                  return el;
                }
              }
              return null;
            };

            const deadline = Date.now() + budgetMs;
            let previous = signal();
            let reached = 0;
            let batches = 0;
            let rateLimited = false;
            let passes = 0;
            let pagesSeen = 1;
            let target = scrollTarget();

            // One page's worth of scrolling. Runs once for a board that does
            // not paginate, which is the whole of the old behaviour.
            const scrollThisPage = async () => {
            let stalls = 0;
            for (let n = 0; n < maxPasses && Date.now() < deadline; n += 1) {
              if (limited()) { rateLimited = true; return; }
              passes += 1;
              target = scrollTarget();
              try {
                target.scrollTop = target.scrollHeight;
              } catch (_) { /* not scrollable after all */ }
              // The window too, in case the container guess was wrong.
              window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
              reached = Math.max(reached, target.scrollTop || window.scrollY || 0);

              // Two seconds for the batch to land, checked often so a fast
              // board is not held to a slow board's pace.
              let grew = false;
              for (let waited = 0; waited < 2000; waited += 200) {
                await new Promise((r) => setTimeout(r, 200));
                if (signal() > previous) { grew = true; break; }
              }

              if (grew) {
                previous = signal();
                batches += 1;
                stalls = 0;
                // Breathing room between batches. The limit is a rate, so the
                // fix for hitting it is a slower hand rather than a smaller
                // total — this buys depth that stopping short would not.
                if (pauseMs > 0) {
                  await new Promise((r) => setTimeout(r, pauseMs));
                }
                continue;
              }
              // A stall is the usual moment for the message to appear: the
              // batch did not arrive *because* the board refused it.
              if (limited()) { rateLimited = true; return; }
              // Three stalls rather than one: these lists pause on a slow
              // request and then carry on, and giving up on the first quiet
              // moment is how a deep scroll turns into a shallow one.
              stalls += 1;
              if (stalls >= 3) return;
            }
            };

            await scrollThisPage();

            // Then the pages behind this one, for a board that has them.
            // `maxPages` is 1 unless the server says otherwise, so a scrolling
            // board never enters this loop at all.
            for (let page = 1; page < maxPages && !rateLimited; page += 1) {
              if (Date.now() >= deadline) break;
              const control = nextControl();
              if (!control) break;   // the last page, or no control we can see

              const before = fingerprint();
              try {
                control.click();
              } catch (_) {
                break;
              }

              // Wait for the list to actually turn over. A click that changes
              // nothing means the control was not what we took it for, and
              // clicking it repeatedly would be the same page harvested over
              // and over — worse than stopping, because it looks like depth.
              let turned = false;
              for (let waited = 0; waited < 6000; waited += 250) {
                await new Promise((r) => setTimeout(r, 250));
                if (fingerprint() !== before) { turned = true; break; }
              }
              if (!turned) break;

              pagesSeen += 1;
              previous = signal();
              if (pauseMs > 0) {
                await new Promise((r) => setTimeout(r, pauseMs));
              }
              // Each new page gets the same treatment: it may lazy-load its
              // own rows as you move down it.
              await scrollThisPage();
            }

            // What this page offers by way of navigation, gathered only when
            // the visit did not get anywhere. The server sends it to a model
            // to work out how this board paginates, so it describes the
            // controls rather than copying the document: a bounded list of
            // things that might be a "next" button, and what scrolling did.
            const describeNavigation = () => {
              const controls = [];
              const seen = new Set();
              for (const el of document.querySelectorAll(
                "a, button, [role='button'], [aria-label]",
              )) {
                if (controls.length >= 40) break;
                const label = (
                  el.getAttribute("aria-label") ||
                  el.getAttribute("title") ||
                  el.textContent ||
                  ""
                ).trim().slice(0, 40);
                // Only things short enough to be a control. A whole job card
                // is an anchor too, and forty of those describe nothing.
                if (!label || label.length > 24) continue;
                // Numbers, next-ish words, arrows. The point is to give the
                // model the pagination row, not the whole navigation bar.
                if (!/^(\d{1,4}|next|prev|previous|older|newer|more|load more|show more|first|last|\u203a|\u00ab|\u00bb|\u2039|\u2192|\u2190|>|<|\.\.\.)$/i
                      .test(label)) continue;
                const key = label + "|" + el.tagName + "|" +
                  String(el.className || "").slice(0, 40);
                if (seen.has(key)) continue;
                seen.add(key);
                controls.push({
                  tag: el.tagName.toLowerCase(),
                  text: (el.textContent || "").trim().slice(0, 40),
                  aria: (el.getAttribute("aria-label") || "").slice(0, 60),
                  title: (el.getAttribute("title") || "").slice(0, 60),
                  rel: (el.getAttribute("rel") || "").slice(0, 20),
                  role: (el.getAttribute("role") || "").slice(0, 20),
                  cls: String(el.className || "").slice(0, 80),
                  id: (el.id || "").slice(0, 60),
                  testid: (el.getAttribute("data-testid") || "").slice(0, 60),
                  href: (el.getAttribute("href") || "").slice(0, 120),
                  disabled: Boolean(el.disabled) ||
                    el.getAttribute("aria-disabled") === "true",
                });
              }

              const query = {};
              try {
                new URL(location.href).searchParams.forEach((value, key) => {
                  query[key] = String(value).slice(0, 40);
                });
              } catch (_) { /* nothing to read */ }

              const doc = document.scrollingElement || document.documentElement;
              return {
                controls,
                query,
                scroll: {
                  passes,
                  batches,
                  doc_height: doc ? doc.scrollHeight : 0,
                  client_height: doc ? doc.clientHeight : 0,
                },
              };
            };

            const text = (document.body ? document.body.innerText : "").slice(0, 4000);
            return {
              title: document.title || "",
              // Only when the visit was unproductive. A crawl that worked has
              // nothing to teach, and sending this every time would be a
              // description of somebody's page on every single visit.
              navigation: (pagesSeen <= 1 && batches <= 1 && !rateLimited)
                ? describeNavigation() : null,
              // A login wall renders instead of the posting, so the harvest
              // finds nothing and looks identical to a reader that broke.
              wall: /sign in|join now to see|log in to continue/i.test(text),
              scrolled: reached,
              // Whether the board asked us to slow down, and how far we got
              // before it did. The second number is what makes the next visit
              // smarter rather than identical.
              rate_limited: rateLimited,
              passes: passes,
              // How many result pages were reached. One means either a board
              // that does not paginate or a "next" control we could not find —
              // and on a board the server *said* paginates, that difference is
              // the whole diagnosis.
              pages: pagesSeen,
              // How many times new content actually arrived. This is the
              // number that says whether the scroll worked: a deep crawl that
              // reports one batch did not scroll, whatever the pixels say.
              batches: batches,
              // Which element was scrolled, so a wrong guess is diagnosable
              // rather than invisible.
              target: (target && target.tagName ? target.tagName : "?") +
                      "." + String((target && target.className) || "").slice(0, 40),
            };
          },
        });
        title = (injected && injected.result && injected.result.title) || "";
        signed_in = !(injected && injected.result && injected.result.wall);
        scrolled = (injected && injected.result && injected.result.scrolled) || 0;
        batches = (injected && injected.result && injected.result.batches) || 0;
        rate_limited = Boolean(
          injected && injected.result && injected.result.rate_limited);
        passes_done = (injected && injected.result && injected.result.passes) || 0;
        pages_done = (injected && injected.result && injected.result.pages) || 0;
        navigation = (injected && injected.result && injected.result.navigation) || null;
        scrollTargetSeen = (injected && injected.result && injected.result.target) || "";
      } catch (_) {
        // Injection refused. The visit still happened, which is the part that
        // matters — the interceptor runs whether or not this could look.
      }

      await new Promise((r) => setTimeout(r, Math.round(settleMs / 2)));

      const tab = await chrome.tabs.get(tabId).catch(() => null);
      return {
        final_url: (tab && tab.url) || url,
        signed_in, title, scrolled, batches,
        scroll_target: scrollTargetSeen,
        rate_limited, passes_done, pages_done, navigation,
        // "" when the page never asked. Reported rather than swallowed so the
        // server can tell a site that blocked us from one that had nothing on
        // it — before this they were the same empty result.
        challenge,
      };
    } finally {
      await chrome.windows.remove(win.id).catch(() => {});
    }
  });
}

// ---------------------------------------------------------------------------
// Anti-bot checks
// ---------------------------------------------------------------------------

/*
 * Some sites put a "verify you are human" interstitial in front of the page.
 * Jooble does it on its `away` redirects, which is exactly the URL link
 * resolution has to open.
 *
 * The window is opened minimized and closed a few seconds later, so the check
 * appeared and vanished with nobody able to click it — the visit failed every
 * time, and looked from the panel like a page that simply had nothing on it.
 *
 * What this does is show the window and wait for *you*. It does not click
 * anything, read the challenge, or try to look like a browser it isn't:
 * solving the check is the user's to do, and the only bug was never giving
 * them the chance. Passing one usually sets a cookie good for a while, so the
 * click buys more than the single page it was spent on.
 */

const CHALLENGE_POLL_MS = 1000;

/*
 * Hosts that showed a check nobody answered, this service-worker lifetime.
 *
 * Without this, sixty queued Jooble pages would each raise a window and hold
 * the tab lock for the full wait — an afternoon of browsing spent on an empty
 * chair, with every other board stuck behind it. The first timeout is taken as
 * the answer: nobody is here, stop asking. The server's own backoff is the
 * durable half; this only stops the damage inside one run.
 */
const challengeGaveUp = new Set();

/**
 * Whether the tab is currently showing an anti-bot interstitial.
 *
 * Returns null when the page cannot be read at all — no host permission, a
 * PDF, a download. Null is deliberately not "there is a challenge": guessing
 * would raise a window at the user for every page we simply cannot see into.
 */
async function challengeShowing(tabId) {
  try {
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const title = (document.title || "").trim();
        const text = (document.body ? document.body.innerText : "").slice(0, 2000);
        // Three independent signals. The markup selectors are the reliable
        // ones; the title and the wording are there for the variants that
        // render inside an iframe this cannot reach into.
        const markup = !!document.querySelector(
          "#challenge-form, #challenge-running, #challenge-stage, " +
            "#cf-challenge-running, #cf-wrapper, .cf-browser-verification, " +
            "input[name='cf_captcha_kind'], " +
            ".cf-turnstile, iframe[src*='challenges.cloudflare.com'], " +
            "iframe[title*='challenge' i], " +
            "script[src*='/cdn-cgi/challenge-platform/']",
        );
        const named = /^(just a moment|attention required|access denied|verifying|please wait)/i
          .test(title);
        const worded =
          /verify (that )?you (are|'re) (a )?human|checking your browser|confirm you are human|complete the security check|needs to review the security of your connection|performance (&|and) security by cloudflare|enable javascript and cookies to continue/i
            .test(text);
        // Whether this could still *become* a challenge. Cloudflare injects
        // its widget into a near-empty shell, so a page with almost no content
        // is one worth looking at again in a moment — and a page full of job
        // cards is not, which is what makes polling affordable.
        const thin =
          text.length < 1500 && document.querySelectorAll("a[href]").length < 15;
        return { challenge: markup || named || worded, title, thin };
      },
    });
    return injected && injected.result ? injected.result : null;
  } catch (_) {
    return null;
  }
}

/**
 * Watch for a challenge to appear, rather than deciding on one look.
 *
 * A single check at a fixed moment was the bug. Cloudflare's interactive
 * widget arrives in an iframe some seconds after `load` fires, and its shell
 * page reloads itself on the way — so the one look landed on an empty document
 * that named nothing, concluded there was no challenge, read the HTML and
 * closed the window. From the outside that is a window that flashes open and
 * shuts before anyone can click it, which is exactly what it looked like.
 *
 * Cheap despite the loop: it returns the moment the page turns out to have
 * real content, because a page full of job cards is not about to become an
 * interstitial. Only a near-empty document is worth waiting on.
 */
async function awaitChallenge(tabId, budgetMs) {
  const deadline = Date.now() + budgetMs;
  let seen = await challengeShowing(tabId);
  while (true) {
    if (!seen) return null;                 // cannot read the page at all
    if (seen.challenge) return seen;        // found it
    if (!seen.thin) return seen;            // a real page; it will not become one
    if (Date.now() >= deadline) return seen;
    await new Promise((r) => setTimeout(r, 700));
    seen = await challengeShowing(tabId);
  }
}

/** Whether fetched markup is an anti-bot interstitial rather than the page. */
function looksLikeChallengeHtml(html) {
  if (!html || html.length > 200000) return false;
  return /cdn-cgi\/challenge-platform|cf-browser-verification|cf_chl_opt|challenges\.cloudflare\.com|<title>\s*just a moment/i
    .test(html);
}

async function challengesAreMine() {
  const { solveChecks } = await chrome.storage.local.get({ solveChecks: true });
  return Boolean(solveChecks);
}

function challengeWaitMs() {
  return CHALLENGE_WAIT_MS;
}

/**
 * Show the window and wait for the user to satisfy the check.
 *
 * Returns "passed", "timeout", or "skipped". The caller keeps going either
 * way — a page that is still a challenge harvests nothing, which is the same
 * outcome as before, only now it is reported as a check rather than as an
 * empty page.
 */
async function waitForHuman(win, tabId, url) {
  // `hostOf` answers null for anything it cannot parse. Falling back to the
  // URL keeps the give-up set keyed on *something* — one entry per URL rather
  // than per host, which errs towards asking again rather than towards a
  // silent `null` bucket that would mute every unparseable page at once.
  const host = hostOf(url) || url;
  if (!(await challengesAreMine())) return "skipped";
  if (challengeGaveUp.has(host)) return "skipped";

  // Raised deliberately: a minimized window cannot be clicked, and the whole
  // failure was that nobody could reach it.
  await chrome.windows
    .update(win.id, { state: "normal", focused: true })
    .catch(() => {});
  await setStatus({
    lastError: `${host} is asking you to confirm you're human — ` +
      `a window is open, click it and browsing carries on.`,
  });

  const deadline = Date.now() + challengeWaitMs();
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, CHALLENGE_POLL_MS));
    const seen = await challengeShowing(tabId);
    // Null means the page stopped being readable — usually the challenge
    // redirecting onward, which is what passing it looks like.
    if (!seen || !seen.challenge) {
      await setStatus({ lastError: "" });
      return "passed";
    }
  }

  challengeGaveUp.add(host);
  await setStatus({
    lastError: `${host} asked for a human check and nobody answered — ` +
      `not asking again this session.`,
  });
  return "timeout";
}

async function openInTab(url) {
  return withTabLock(async () => {
    let win;
    try {
      win = await chrome.windows.create({
        url,
        focused: false,
        state: "minimized",
      });
    } catch (error) {
      throw new Error(`could not open a window: ${error.message}`);
    }

    const tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
    if (!tabId) {
      await chrome.windows.remove(win.id).catch(() => {});
      throw new Error("the window opened without a tab.");
    }

    try {
      await waitForLoad(tabId, TAB_LOAD_TIMEOUT_MS);
      await new Promise((r) => setTimeout(r, TAB_SETTLE_MS));

      // The path this matters most on. A Jooble `away` link is a redirect to
      // the employer, and the check sits in front of the redirect — so being
      // stopped here does not cost a description, it costs the apply URL
      // itself, which is the one thing that page existed to give us.
      // Up to eight seconds, and it exits the moment the page proves to be
      // real. This path is an escalation that already failed once, so it is
      // not on anybody's hot path and can afford to be patient.
      let challenge = "";
      const gate = await awaitChallenge(tabId, 8000);
      if (gate && gate.challenge) {
        challenge = await waitForHuman(win, tabId, url);
        if (challenge === "passed") {
          await waitForLoad(tabId, TAB_LOAD_TIMEOUT_MS).catch(() => {});
          await new Promise((r) => setTimeout(r, TAB_SETTLE_MS));
        }
      }

      const tab = await chrome.tabs.get(tabId);
      let html = "";
      let text = "";
      try {
        const [injected] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => ({
            html: document.documentElement ? document.documentElement.outerHTML : "",
            text: document.body ? document.body.innerText : "",
          }),
        });
        html = (injected && injected.result && injected.result.html) || "";
        text = (injected && injected.result && injected.result.text) || "";
      } catch (_) {
        // Reading the page failed — a PDF, a download, a page that refuses
        // injection. The landing URL alone is still worth reporting.
      }

      return {
        final_url: tab.url || url,
        html: html.slice(0, 400000),
        text: text.slice(0, 400000),
        via: "tab",
        challenge,
      };
    } finally {
      await chrome.windows.remove(win.id).catch(() => {});
    }
  });
}

/** Whether a failed fetch is worth reopening as a real page. */
function worthEscalating(message) {
  // 403/429 and network-level refusals are the shapes that mean "not like
  // this". A 404 is the page genuinely not being there, and reopening it in a
  // window would only cost the user a flicker to learn the same thing.
  return /HTTP (401|403|405|406|429|503)\b|Failed to fetch|NetworkError|challenge page from/i
    .test(message);
}

async function tabsAllowed() {
  const { useTabs } = await chrome.storage.local.get({ useTabs: true });
  return useTabs;
}

// ---------------------------------------------------------------------------
// Doing the work
// ---------------------------------------------------------------------------

/**
 * What this agent can actually run.
 *
 * `ping` earns its place by depending on nothing: it proves lease → execute →
 * report works without any website being reachable or any session being valid,
 * so a broken handler stays distinguishable from a broken protocol.
 */
const HANDLERS = {
  async ping(payload) {
    return { pong: true, echo: payload ?? {}, at: new Date().toISOString() };
  },

  /**
   * Fetch a JSON endpoint the server is walled out of.
   *
   * Reddit answers a datacenter IP with 403 Blocked — a categorical refusal
   * rather than a rate limit, so no amount of retrying from the VPS helps. From
   * here it is a browser on a home connection asking for a public page, which
   * is the entire premise of this queue.
   *
   * Cookies are omitted. These are public endpoints, and sending the user's
   * session to them would be an unnecessary widening of what a queued URL can
   * reach.
   */
  async fetch_json(payload) {
    const url = payload && payload.url;
    if (!url) throw new Error("fetch_json needs a url.");

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {
        method: "GET",
        // Cookies for the sites that need a session to answer at all, omitted
        // everywhere else. See SESSION_HOSTS — the decision is made here from
        // the URL rather than taken from the task, so a queued URL can never
        // ask for your cookies to be sent somewhere new.
        credentials: sendsCredentialsTo(url) ? "include" : "omit",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });

      const text = (await response.text()).slice(0, 2000000);

      if (!response.ok) {
        // The body, not just the status. A 403 is a block page, a rate limit,
        // or a demand to log in, and those have three different fixes — the
        // number alone sent us looking in the wrong place once already.
        const hint = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 300);
        const message =
          `HTTP ${response.status} from ${new URL(url).host}` + (hint ? ` — ${hint}` : "");

        // Same escalation as resolve_link: a JSON endpoint that refuses a
        // background fetch will usually render for a real page load, and
        // `innerText` on the result is the same document.
        if (worthEscalating(message) && (await tabsAllowed())) {
          const opened = await openInTab(url);
          try {
            return { status: 200, json: JSON.parse(opened.text), via: "tab" };
          } catch (_) {
            throw new Error(`${message} (also unreadable when opened as a page)`);
          }
        }
        throw new Error(message);
      }

      try {
        return { status: response.status, json: JSON.parse(text) };
      } catch (_) {
        // Not JSON after all — hand back the text so the server can say why
        // rather than failing with "unparseable" and nothing to look at.
        return { status: response.status, json: null, text: text.slice(0, 4000) };
      }
    } finally {
      clearTimeout(timer);
    }
  },

  /**
   * Follow an aggregator redirect to the employer's real apply page.
   *
   * The server tries this first and gets most of them. What reaches here is
   * what a datacenter IP could not follow — Indeed and Glassdoor answer those
   * with an interstitial or a challenge rather than a redirect. From here it is
   * an ordinary browser making an ordinary request, which is the entire point.
   *
   * `fetch` follows redirects on its own and `response.url` is where it landed,
   * so the hop chain needs no manual walking. The body comes back too: even
   * when the landing page is another aggregator and therefore not an apply
   * link, it often names the company's Greenhouse or Lever board, which the
   * server mines separately.
   */
  /**
   * Open a page, let it run, close it. The harvest does the rest.
   *
   * Nothing useful comes back through this handler and that is the design.
   * The interceptor is registered on the site by match pattern, so it runs on
   * this tab exactly as it does on one you opened yourself — the page asks its
   * own API for the posting, the interceptor reads the answer on the way past,
   * and the jobs arrive at /api/agent/harvest under their own steam. All this
   * has to do is make the visit happen.
   *
   * Which is why the settle is long and the gap after it is longer. LinkedIn
   * fires the request carrying the posting body *after* `load`, so a tab closed
   * promptly harvests nothing at all — and a browser stepping through sixty
   * pages back to back is the shape of traffic that gets a logged-in session
   * challenged. The server sets both numbers so the pace is one decision in one
   * place, and this only clamps them against a client that would otherwise be
   * told to hammer.
   */
  async browse_page(payload) {
    const url = payload && payload.url;
    if (!url) throw new Error("browse_page needs a url.");
    if (!(await tabsAllowed())) {
      throw new Error(
        "Opening pages in a hidden window is turned off in the extension's options.",
      );
    }

    // Unticking a site is meant to mean "leave this site alone", and until now
    // it only stopped the *reading*: the pages kept being opened and simply
    // harvested nothing. That is the worst of both — same traffic through the
    // same logged-in session, no data for it — and it matters most in exactly
    // the case the checkbox exists for, which is a site that has warned you
    // about the volume.
    const site = siteForUrl(url);
    if (site) {
      const stored = await chrome.storage.local.get({
        [site.storageKey]: false,
      });
      if (!stored[site.storageKey]) {
        throw new Error(
          `${site.label} is turned off in the extension's options, so this ` +
            `page was not opened.`,
        );
      }
    }

    const settleMs = clampSeconds(payload.settle_seconds, 6, 1, 60) * 1000;
    const gapMs = clampSeconds(payload.gap_seconds, 20, 5, 300) * 1000;
    // How far to scroll. On a board that paginates by URL this is a handful of
    // screens; on one that scrolls infinitely it is the pagination, so the
    // server asks for a lot more and the budget above decides when to stop.
    const passes = clampSeconds(payload.scroll_passes, 25, 1, 400);
    // How long to rest between batches. An infinite list rate-limits on a
    // rate, so a slower hand reaches further than a shorter run does — the
    // server sets it per board because only it knows which boards have
    // complained.
    const pauseMs = clampSeconds(payload.scroll_pause_seconds, 0, 0, 10) * 1000;
    // Result pages to click through. One unless the server says this board
    // paginates, so a scrolling board behaves exactly as it always has.
    const maxPages = clampSeconds(payload.max_pages, 1, 1, 50);
    // A next-page selector the server learned for this board. Empty for a
    // board nothing has been learned about, which is where the extension's own
    // heuristics apply.
    const clickSelector = String(payload.click_selector || "").slice(0, 200);

    const visited = await visitInTab(url, settleMs, passes, pauseMs, maxPages,
                                     clickSelector);
    // Held inside the tab lock's queue by awaiting here: the next browse task
    // cannot open its window until this pause is over, which is what makes the
    // gap a real rhythm rather than a number in a payload.
    await new Promise((resolve) => setTimeout(resolve, gapMs));

    return {
      final_url: visited.final_url,
      scrolled_px: visited.scrolled,
      batches: visited.batches,
      scroll_target: visited.scroll_target,
      // Whether the visit looked like a real page rather than a login wall or
      // a challenge. The server cannot tell from a harvest that found nothing.
      signed_in: visited.signed_in,
      title: visited.title,
      settled_ms: settleMs,
      // "", "passed", "timeout" or "skipped". The distinction the panel needs:
      // a site that asked for a human check is not a site whose field names
      // moved, and telling the user to go fix the reader would be wrong.
      challenge: visited.challenge,
      // The board asked us to wait. Reported rather than swallowed so the
      // server can rest that host and come back, instead of sending the next
      // sixty pages into the same closed door.
      rate_limited: visited.rate_limited,
      // How deep the scroll got. On an infinite list this is the honest
      // measure of how much of the board a visit covered, and when it ends in
      // a limit it is also the depth this board will tolerate.
      passes_done: visited.passes_done,
      // Result pages reached. On a board the server asked to paginate, a 1
      // here means the "next" control was not found — a different problem
      // from a board that had nothing on it, needing a different fix, so it
      // is reported rather than folded into the other numbers.
      pages_done: visited.pages_done,
      // Present only when the visit got nowhere: the controls this page
      // offers, so the server can work out how the board paginates instead of
      // waiting to be told.
      navigation: visited.navigation,
    };
  },

  /**
   * Open a site so you can pass its check, and leave it open.
   *
   * Every other tab path here is built to be unobtrusive: minimized, closed
   * again, gone before you notice. That is exactly wrong for the one case
   * where the whole point is that a person has to see the page and click
   * something, and it is why the check "closed too fast to click" however good
   * the detection got.
   *
   * So this one detects nothing, polls nothing, and closes nothing. It opens a
   * normal focused tab and finishes. You click the checkbox in your own time
   * and close the tab yourself; the clearance cookie is set in the browser's
   * cookie jar, and every later request carries it — which is what makes one
   * click worth something rather than being spent on a single link.
   *
   * It is also immune to the service worker being terminated mid-wait, which
   * a ninety-second poll is not: by the time the worker can be killed, this
   * has already done everything it was going to do.
   */
  async pass_check(payload) {
    const url = payload && payload.url;
    if (!url) throw new Error("pass_check needs a url.");
    if (!(await tabsAllowed())) {
      throw new Error(
        "Opening pages is turned off in the extension's options.",
      );
    }

    const tab = await chrome.tabs.create({ url, active: true });
    try {
      await chrome.windows.update(tab.windowId, { focused: true });
    } catch (_) {
      /* the window is there either way */
    }
    await setStatus({
      lastError: `Opened ${new URL(url).host} in a tab — pass its check ` +
        `there, then close the tab. Nothing else needs doing.`,
    });
    return { opened: url, left_open: true };
  },

  async resolve_link(payload) {
    const url = payload && payload.url;
    if (!url) throw new Error("resolve_link needs a url.");

    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(url, {
          method: "GET",
          redirect: "follow",
          // Cookies included, and this is the fix for Jooble rather than a
          // convenience.
          //
          // Passing a Cloudflare check sets a clearance cookie, and that
          // cookie is the *only* evidence that you passed. Omitting it meant
          // every request arrived as a first-time visitor and was challenged
          // again — so the check could be solved perfectly and the next link
          // would be challenged identically, forever. No amount of better
          // detection fixes that; the proof was being thrown away on the way
          // out.
          //
          // The earlier reasoning here — that this would leak the user's
          // sessions to an arbitrary aggregator — does not hold up. Cookies go
          // to the origin they belong to and nowhere else: a request to
          // jooble.org carries jooble.org's cookies to jooble.org. Following a
          // redirect gives each hop its own, which is exactly what the browser
          // does when you click the link yourself.
          credentials: "include",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status} from ${new URL(url).host}`);

        const contentType = response.headers.get("content-type") || "";
        // Cap the body. Some landing pages are enormous, and everything the
        // server mines out of one is in the markup near the top.
        const html = contentType.includes("html")
          ? (await response.text()).slice(0, 400000)
          : "";

        // A challenge served with a 200. Cloudflare's managed challenge often
        // is, and `response.ok` is then true — so this returned the
        // interstitial as though it were the employer's page, and the tab path
        // that offers you the checkbox was never reached. The escalation rule
        // below only ever looked at status codes, which cannot see this.
        if (looksLikeChallengeHtml(html)) {
          throw new Error(`challenge page from ${new URL(url).host}`);
        }

        return { final_url: response.url, status: response.status, html };
      } finally {
        clearTimeout(timer);
      }
    } catch (error) {
      // Aggregators refuse the fetch shape, not this browser. Opening the URL
      // as a real page gets the redirect chain, the JavaScript and the
      // meta-refresh that a service-worker fetch never sees.
      const message = String(error && error.message ? error.message : error);
      if (!worthEscalating(message) || !(await tabsAllowed())) throw error;
      return await openInTab(url);
      // `openInTab` returns `challenge` alongside the URL and markup, so a
      // link that could not be followed because somebody has to click
      // something says so rather than coming back as an ordinary failure.
    }
  },
};

/**
 * The permission `resolve_link` needs: fetching arbitrary job sites.
 *
 * Deliberately not requested at install. Reaching your own server is one thing;
 * reading any page on the web is a materially larger ask, and it should be a
 * separate, revocable decision rather than something bundled into setup.
 */
const BROAD_HOSTS = { origins: ["https://*/*", "http://*/*"] };

/**
 * Hosts that get the browser's cookies on a queued fetch.
 *
 * Reddit refuses anonymous JSON outright — 403 whoever asks, residential IP or
 * not — so the only thing that makes the browser more useful than the server
 * here is the session you already have. That is the extension's entire premise,
 * and omitting cookies threw it away.
 *
 * Decided here from the URL rather than read out of the task, so that the
 * server can never queue a URL that causes your cookies to be sent somewhere
 * this list does not already name.
 */
const SESSION_HOSTS = ["reddit.com", "www.reddit.com", "old.reddit.com"];

function sendsCredentialsTo(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return SESSION_HOSTS.some((allowed) => host === allowed || host.endsWith(`.${allowed}`));
  } catch (_) {
    return false;
  }
}

/**
 * Whether arbitrary sites are reachable.
 *
 * Checked one origin at a time and satisfied by either. `permissions.contains`
 * with both listed demands both, and Chrome does not always end up holding both
 * after a request — so asking for the pair answers "false" while https is in
 * fact granted, and the agent then quietly stops claiming work it could do.
 * Everything queued here is https in practice.
 */
async function canReachTheWeb() {
  for (const origin of BROAD_HOSTS.origins) {
    try {
      if (await chrome.permissions.contains({ origins: [origin] })) return true;
    } catch (_) {
      /* asking about one origin failing should not veto the other */
    }
  }
  return false;
}

/**
 * What this agent will actually claim right now.
 *
 * Permission-aware on purpose: leasing a kind we cannot run would take the task
 * out of the queue only to fail it, three times, until it retires. Not asking
 * for it leaves it queued for an engine that can.
 */
/**
 * Hosts the harvest reader is registered on right now.
 *
 * Both halves matter: the checkbox can be ticked with the host permission
 * refused, or granted and then revoked, and either leaves the reader not
 * running on a site the options page shows as on.
 */
async function enabledHarvestHosts() {
  const keys = Object.fromEntries(
    HARVEST_SITES.map((site) => [site.storageKey, false]),
  );
  const stored = await chrome.storage.local.get(keys);

  const hosts = [];
  for (const site of HARVEST_SITES) {
    if (!stored[site.storageKey]) continue;
    let permitted = false;
    try {
      permitted = await chrome.permissions.contains({ origins: site.matches });
    } catch (_) {
      permitted = false;
    }
    if (!permitted) continue;
    for (const pattern of site.matches) {
      const host = /^https?:\/\/([^/]+)/.exec(pattern);
      if (host) hosts.push(host[1].replace(/^\*\./, ""));
    }
  }
  return hosts;
}

async function supportedKinds() {
  const kinds = ["ping"];
  if (await canReachTheWeb()) kinds.push("resolve_link", "fetch_json");
  // Browsing needs a window rather than a host permission — opening a tab is
  // not reading a page, and the reading is the interceptor's, under the
  // permission its own checkbox already asked for. So the toggle that governs
  // opening windows at all is the one that decides this.
  if (await tabsAllowed()) kinds.push("browse_page", "pass_check");
  return kinds;
}

async function runTask(task) {
  const handler = HANDLERS[task.kind];
  if (!handler) throw new Error(`This agent cannot run ${task.kind} tasks.`);
  return await handler(task.payload);
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

let polling = false;

async function pollOnce() {
  // Alarms can overlap a chained poll that is still running. Two concurrent
  // loops would lease two batches and race each other's reports.
  if (polling) return;
  polling = true;

  try {
    const config = await getConfig();
    if (!config.enabled || !config.serverUrl || !config.token) return;

    const id = await agentId();
    const kinds = await supportedKinds();
    // Which sites the reader is actually registered on. The server cannot
    // know this and had to guess, so a board that opened pages and forwarded
    // nothing got the same two-part shrug either way — "the box is unticked,
    // or its pages fetch jobs from somewhere we don't watch". Those want
    // completely different fixes and only this side knows which applies.
    const reading = await enabledHarvestHosts();

    const { tasks } = await api("/api/agent/lease", {
      kinds,
      agent_id: id,
      max: 5,
      wait: 25,
      harvest_sites: reading,
    });

    // Recorded locally as well as sent, so the options page can show what this
    // install offers without waking the worker to ask.
    await setStatus({
      lastPoll: new Date().toISOString(),
      lastError: null,
      kinds,
    });
    if (!tasks || tasks.length === 0) return;

    let done = 0;
    for (const task of tasks) {
      try {
        const result = await runTask(task);
        await api(`/api/agent/tasks/${task.id}/result`, { result, agent_id: id });
        done += 1;
      } catch (error) {
        // Report the failure rather than dropping it. The server decides what
        // to do with it — but only this side knows whether a retry could ever
        // help, so a refusal is flagged as final. A 403 will be a 403 again,
        // and three identical rows bury whatever else failed that hour.
        const message = String(error && error.message ? error.message : error);
        await api(`/api/agent/tasks/${task.id}/fail`, {
          error: message,
          permanent: /HTTP 4(0[0-9]|1[0-8])\b/.test(message),
          agent_id: id,
        });
      }
    }
    await setStatus({ lastCompleted: done, lastTaskAt: new Date().toISOString() });

    // More work probably waits behind what we just drained, and the next alarm
    // is up to a minute away.
    setTimeout(() => pollOnce(), 0);
  } catch (error) {
    await setStatus({ lastError: String(error && error.message ? error.message : error) });
  } finally {
    polling = false;
  }
}

// ---------------------------------------------------------------------------
// Passive harvest
// ---------------------------------------------------------------------------

/**
 * Harvest reads job JSON from LinkedIn pages you visit anyway. Narrow on
 * purpose — one site, not the whole web — and registered at runtime rather than
 * declared in the manifest, so installing the extension does not ask for
 * LinkedIn access on behalf of a feature that is off.
 */
/* HARVEST_SITES is imported at the top of this file — see sites.js. */

/** Kept for the options page and for anything still reading one site. */
const HARVEST_HOSTS = { origins: ["https://www.linkedin.com/*"] };

function harvestScriptsFor(site) {
  return [
    {
      id: `jobapp-interceptor-${site.id}`,
      matches: site.matches,
      js: ["interceptor.js"],
      runAt: "document_start",
      // MAIN shares the page's globals, which is the only way to patch the
      // `fetch` the page itself calls. It also means no chrome.* here.
      world: "MAIN",
    },
    {
      id: `jobapp-relay-${site.id}`,
      matches: site.matches,
      js: ["relay.js"],
      runAt: "document_start",
    },
  ];
}

async function registeredIds(ids) {
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({ ids });
    return new Set(existing.map((s) => s.id));
  } catch (_) {
    return new Set();
  }
}

async function syncHarvestScripts() {
  const keys = Object.fromEntries(
    HARVEST_SITES.map((site) => [site.storageKey, false]),
  );
  const stored = await chrome.storage.local.get(keys);

  const allIds = HARVEST_SITES.flatMap((site) =>
    harvestScriptsFor(site).map((s) => s.id),
  );
  // The pre-multi-site ids, so an upgrade does not leave a stale pair
  // registered against LinkedIn forever.
  const legacyIds = ["jobapp-interceptor", "jobapp-relay"];
  await chrome.scripting
    .unregisterContentScripts({ ids: legacyIds })
    .catch(() => {});

  const already = await registeredIds(allIds);

  for (const site of HARVEST_SITES) {
    const scripts = harvestScriptsFor(site);
    const ids = scripts.map((s) => s.id);
    const wanted =
      Boolean(stored[site.storageKey]) &&
      (await chrome.permissions.contains({ origins: site.matches }));
    const registered = ids.every((id) => already.has(id));

    try {
      if (wanted && !registered) {
        // Unregister first: a half-registered pair from an interrupted run
        // would otherwise make register() throw on the duplicate id.
        await chrome.scripting.unregisterContentScripts({ ids }).catch(() => {});
        await chrome.scripting.registerContentScripts(scripts);
      } else if (!wanted && registered) {
        await chrome.scripting.unregisterContentScripts({ ids });
      }
    } catch (error) {
      await setStatus({
        lastError: `harvest scripts (${site.id}): ${error.message}`,
      });
    }
  }
}

// ---------------------------------------------------------------------------
// On-page overlay
// ---------------------------------------------------------------------------

/**
 * Job sites the overlay draws on. A list rather than a wildcard: it covers the
 * ATS boards where applications actually happen, and asking for those by name
 * is a smaller and more legible request than "every site you visit".
 */
const OVERLAY_MATCHES = [
  "https://www.linkedin.com/jobs/*",
  "https://boards.greenhouse.io/*",
  "https://job-boards.greenhouse.io/*",
  "https://jobs.lever.co/*",
  "https://jobs.ashbyhq.com/*",
  "https://*.myworkdayjobs.com/*",
  "https://apply.workable.com/*",
  "https://jobs.smartrecruiters.com/*",
  "https://*.recruitee.com/*",
];
const OVERLAY_HOSTS = { origins: OVERLAY_MATCHES };
const OVERLAY_SCRIPTS = [
  {
    id: "jobapp-overlay",
    matches: OVERLAY_MATCHES,
    js: ["overlay.js"],
    runAt: "document_idle",
  },
];

async function syncOverlayScripts() {
  const wanted =
    (await chrome.storage.local.get({ overlay: false })).overlay &&
    (await chrome.permissions.contains(OVERLAY_HOSTS));
  let registered = false;
  try {
    registered =
      (await chrome.scripting.getRegisteredContentScripts({ ids: ["jobapp-overlay"] }))
        .length > 0;
  } catch (_) {
    registered = false;
  }

  try {
    if (wanted && !registered) {
      await chrome.scripting
        .unregisterContentScripts({ ids: ["jobapp-overlay"] })
        .catch(() => {});
      await chrome.scripting.registerContentScripts(OVERLAY_SCRIPTS);
    } else if (!wanted && registered) {
      await chrome.scripting.unregisterContentScripts({ ids: ["jobapp-overlay"] });
    }
  } catch (error) {
    await setStatus({ lastError: `overlay scripts: ${error.message}` });
  }
}

/**
 * The overlay's only route to the server.
 *
 * It cannot call the API itself: the token lives in extension storage, and a
 * content script that held it would be handing a credential to whatever page it
 * is running on. So the panel asks, this fetches, and the token never enters
 * the page's process.
 */
async function overlayApi(path, body) {
  const config = await getConfig();
  if (!config.serverUrl || !config.token) {
    return { error: "Set your server URL and token in the extension options." };
  }
  try {
    const data = await api(path, body);
    return { data, serverUrl: config.serverUrl };
  } catch (error) {
    return { error: error.message, serverUrl: config.serverUrl };
  }
}

// ---------------------------------------------------------------------------
// Event reporting
// ---------------------------------------------------------------------------

/**
 * Tell the server what happened, and remember it here either way.
 *
 * Two destinations because they answer different questions. The server's copy
 * is the one that survives a reinstall and can be counted over weeks. The local
 * ring buffer is what the options page shows, and it is the only thing that
 * works when the server is the thing that is broken — which is exactly when
 * somebody opens the options page.
 *
 * Nothing here throws. Reporting that an autofill happened must never be able
 * to break the autofill.
 */
const EVENT_BUFFER_SIZE = 50;

async function reportEvent(kind, { url, ok = true, summary } = {}) {
  const entry = {
    kind,
    // The host, never the page. The server stores it that way for the same
    // reason: this is a diagnostic, not a browsing history.
    host: hostOf(url),
    ok,
    summary: summary || {},
    at: new Date().toISOString(),
  };

  try {
    const stored = (await chrome.storage.local.get("events")).events || [];
    await chrome.storage.local.set({
      events: [entry, ...stored].slice(0, EVENT_BUFFER_SIZE),
    });
  } catch (_) {
    /* storage full or unavailable; the server copy is still worth trying */
  }

  try {
    const config = await getConfig();
    if (!config.serverUrl || !config.token) return;
    await api("/api/agent/report", {
      agent_id: await agentId(),
      events: [entry],
    });
  } catch (_) {
    // Offline is the normal state for half the day. The local buffer already
    // has it, and losing one diagnostic event is not worth a retry queue.
  }
}

function hostOf(url) {
  if (!url) return null;
  try {
    return new URL(url).hostname || null;
  } catch (_) {
    return null;
  }
}

async function forwardHarvest(payload, sourceUrl, probe) {
  const config = await getConfig();
  if (!config.serverUrl || !config.token) return;
  try {
    const counts = await api("/api/agent/harvest", {
      payload,
      source_url: sourceUrl,
      probe: Boolean(probe),
      // So the server files the event under the browser it came from. Several
      // can be running, and "harvest stopped working" is usually only true of
      // one of them.
      agent_id: await agentId(),
    });
    if (counts && counts.found) {
      await setStatus({
        lastHarvest: new Date().toISOString(),
        lastHarvestFound: counts.found,
        lastHarvestNew: counts.inserted || 0,
      });
    }
  } catch (error) {
    await setStatus({ lastError: `harvest: ${error.message}` });
  }
}

function ensureAlarm() {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  syncHarvestScripts();
  syncOverlayScripts();
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  syncHarvestScripts();
  syncOverlayScripts();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) pollOnce();
});

// The options page asks for an immediate poll after you enable the agent, so
// that "on" and "working" are the same moment rather than a minute apart.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "harvest") {
    // Only from a tab we injected into. A message with no tab came from an
    // extension page, which has no business offering harvested jobs.
    if (!sender.tab) return false;
    forwardHarvest(message.payload, message.sourceUrl, message.probe);
    return false;
  }
  if (message?.type === "sync-harvest") {
    Promise.all([syncHarvestScripts(), syncOverlayScripts()]).then(() =>
      sendResponse({ ok: true }),
    );
    return true;
  }
  if (message?.type === "overlay-api") {
    if (!sender.tab) return false;
    overlayApi(message.path, message.body).then(sendResponse);
    return true;
  }
  if (message?.type === "overlay-event") {
    // From the panel, which knows what it did — whether a fill matched
    // anything, whether the resume went in. The service worker cannot see any
    // of that, and neither could the server.
    if (!sender.tab) return false;
    reportEvent(message.kind, {
      url: sender.tab.url,
      ok: message.ok !== false,
      summary: message.summary,
    });
    return false;
  }
  if (message?.type === "poll-now") {
    pollOnce().then(() => sendResponse({ ok: true }));
    return true; // keep the channel open for the async reply
  }
  if (message?.type === "supported-kinds") {
    supportedKinds().then((kinds) => sendResponse({ kinds }));
    return true;
  }
  return false;
});

ensureAlarm();
syncHarvestScripts();
syncOverlayScripts();
