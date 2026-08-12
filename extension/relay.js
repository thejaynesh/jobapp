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

window.addEventListener("message", (event) => {
  // Only messages this page sent to itself. Without this, a cross-origin frame
  // could feed the pipeline.
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.channel !== CHANNEL || data.payload === undefined) return;

  chrome.runtime.sendMessage(
    { type: "harvest", payload: data.payload, sourceUrl: data.sourceUrl },
    () => {
      // The service worker may be asleep or mid-restart. Losing one payload is
      // not worth surfacing — the next page view offers more, and there is
      // nothing the user could do about it anyway.
      void chrome.runtime.lastError;
    },
  );
});
