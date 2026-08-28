/**
 * Carries findings from the page's world to the extension's. Isolated world.
 *
 * Two worlds, two halves, because neither can do the other's job: the
 * interceptor must share the page's globals to patch the `fetch` the page
 * actually calls, and that same sharing means it has no `chrome.*` to reach the
 * extension with. This side has `chrome.*` and no access to the page's globals.
 * `window.postMessage` is the only channel between them.
 *
 * Which is also why the checks below exist. `postMessage` is a public channel:
 * any script on the page can send to it, including the page itself. Nothing
 * here is trusted beyond "some JSON arrived" — it is forwarded to the server,
 * which decides what is job-shaped and stores nothing it cannot recognize.
 */

const CHANNEL = "jobapp-harvest";

/**
 * Whether this script can still reach the extension it came from.
 *
 * Reloading an extension leaves its already-injected content scripts running
 * in every open tab with the connection severed. `chrome.runtime.id` becomes
 * `undefined` at that moment and `sendMessage` throws synchronously, so
 * `lastError` — which only reports a delivered call that found no receiver —
 * never gets a chance to say anything.
 *
 * It matters more here than anywhere else in the extension: this fires once
 * per intercepted response, so an orphaned relay throws continuously for as
 * long as the tab stays open, on a site the user is actively browsing.
 */
function connected() {
  try {
    return Boolean(chrome.runtime && chrome.runtime.id);
  } catch (_) {
    return false;
  }
}

function onMessage(event) {
  // Only messages this page sent to itself. Without this, a cross-origin frame
  // could feed the pipeline.
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.channel !== CHANNEL) return;
  const isStats = data.payload === undefined && data.stats;
  if (data.payload === undefined && !isStats) return;

  // Orphaned by a reload. Stop listening rather than fail per response: this
  // tab's harvest is over until the page is refreshed, and the interceptor
  // will keep offering payloads for as long as it is open.
  if (!connected()) {
    window.removeEventListener("message", onMessage);
    return;
  }

  try {
    chrome.runtime.sendMessage(
      isStats
        ? {
            // Not a payload: a count of what the reader looked at on this
            // page. Sent even — especially — when it forwarded nothing, which
            // is the case that otherwise reaches the server as silence.
            type: "harvest-stats",
            stats: data.stats,
            sourceUrl: data.sourceUrl,
          }
        : {
            type: "harvest",
            payload: data.payload,
            sourceUrl: data.sourceUrl,
            // A near miss rather than a recognised job payload. Forwarded so
            // the server has evidence to learn from; marked so it can say so.
            probe: Boolean(data.probe),
          },
      () => {
        // The service worker may be asleep or mid-restart. Losing one payload
        // is not worth surfacing — the next page view offers more, and there
        // is nothing the user could do about it anyway.
        void chrome.runtime.lastError;
      },
    );
  } catch (_) {
    // Invalidated between the check and the call.
    window.removeEventListener("message", onMessage);
  }
}

window.addEventListener("message", onMessage);
