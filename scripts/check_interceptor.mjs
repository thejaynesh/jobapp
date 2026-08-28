/**
 * Drive interceptor.js in a real browser and check two things at once.
 *
 * There is no way to unit-test this file. It patches page globals, runs in the
 * MAIN world, and the behaviour that matters — which frame a Content Security
 * Policy violation is reported against — exists only inside a browser engine.
 * Reasoning about it has been wrong twice.
 *
 * So this loads a page shaped like the ones that caused trouble: a strict
 * `connect-src` header, an analytics beacon to a host that header refuses, and
 * a job payload read four different ways. Then it asserts:
 *
 *   1. The refused beacon is blamed on the *page's own script*. It was blamed
 *      on interceptor.js for as long as this patched `window.fetch`, because
 *      CSP is enforced when a request is initiated and the patch was the
 *      calling frame by definition. Reading the body instead of the request is
 *      what fixed it, and this is what stops that quietly reverting.
 *   2. Every way a page reads a JSON body still reaches us, and reading HTML
 *      does not — that count is what the harvest panel reports.
 *
 * Needs playwright, which the application does not otherwise use, so it is not
 * part of `pytest`. Run it by hand after touching interceptor.js:
 *
 *     npm install playwright && node scripts/check_interceptor.mjs
 */

import http from "node:http";
import fs from "node:fs";
import { chromium } from "playwright";

const PORT = 8099;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const JOBS = { data: { jobList: [{ jobTitle: "Backend Engineer", companyName: "Acme" }] } };

// Shaped like Handshake's: same-origin only, so the page's own ad beacon is
// refused by the page's own policy. That refusal is the thing being attributed.
const CSP = "connect-src 'self'";

const PAGE = `<!doctype html><html><body><h1>board</h1><script>
fetch("https://pagead2.googlesyndication.com/ccm/collect?x=1")
  .then(() => { window.__beacon = "allowed"; })
  .catch((e) => { window.__beacon = "refused"; });

fetch("/api/job-search?page=1").then(r => r.json()).then(d => {
  window.__viaJson = d.data.jobList[0].jobTitle;
});

fetch("/api/text-jobs").then(r => r.text()).then(t => {
  window.__viaText = JSON.parse(t).data.jobList[0].jobTitle;
});

// Read as text, but it is HTML. Must not be counted as a JSON response.
fetch("/page.html").then(r => r.text()).then(t => { window.__viaHtml = t.length; });

const x = new XMLHttpRequest();
x.open("GET", "/api/xhr-jobs");
x.responseType = "json";
x.onload = () => { window.__viaXhr = x.response.data.jobList[0].jobTitle; };
x.send();
</script></body></html>`;

const server = http.createServer((req, res) => {
  const send = (body, type, extra = {}) =>
    res.writeHead(200, { "Content-Type": type, ...extra }).end(body);
  if (req.url.startsWith("/api/text-jobs")) {
    // JSON served as text/plain. The old content-type check missed these.
    send(JSON.stringify(JOBS), "text/plain");
  } else if (req.url.startsWith("/api/")) {
    send(JSON.stringify(JOBS), "application/json");
  } else if (req.url.startsWith("/page.html")) {
    send(PAGE, "text/html", { "Content-Security-Policy": CSP });
  } else {
    res.writeHead(404).end();
  }
});

await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || "/opt/pw-browsers/chromium",
});
const page = await browser.newPage();

const violations = [];
page.on("console", (m) => {
  if (/Content Security Policy|Refused to connect/i.test(m.text())) {
    violations.push(m.location());
  }
});

await page.addInitScript(fs.readFileSync("extension/interceptor.js", "utf8"));

const offered = [];
await page.exposeFunction("__report", (d) => offered.push(d));
await page.addInitScript(() => {
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || e.data.channel !== "jobapp-harvest") return;
    window.__report({ stats: e.data.stats || null, url: e.data.sourceUrl });
  });
});

await page.goto(`${ORIGIN}/page.html`, { waitUntil: "networkidle" });
await page.waitForTimeout(4000);

const state = await page.evaluate(() => ({
  beacon: window.__beacon,
  viaJson: window.__viaJson,
  viaText: window.__viaText,
  viaHtml: window.__viaHtml,
  viaXhr: window.__viaXhr,
}));

await browser.close();
server.close();

const failures = [];
const check = (ok, what) => {
  console.log(`${ok ? "ok  " : "FAIL"}  ${what}`);
  if (!ok) failures.push(what);
};

// An injected script reports an empty URL, so a filename check would pass for
// the wrong reason. The page's own script is the only honest culprit.
const misattributed = violations.filter((v) => !/page\.html/.test(v.url || ""));
check(violations.length > 0, "the beacon was refused (the fixture still works)");
check(
  misattributed.length === 0,
  `the refusal is blamed on the page, not on us` +
    (misattributed.length ? ` — blamed on ${JSON.stringify(misattributed)}` : ""),
);

check(state.beacon === "refused", "the page saw its own request fail, as it should");
check(state.viaJson === "Backend Engineer", "the page's own json() still resolves");
check(state.viaText === "Backend Engineer", "the page's own text() still resolves");
check(state.viaXhr === "Backend Engineer", "the page's own XHR still resolves");
check(state.viaHtml > 0, "the page can still read HTML through text()");

const payloads = offered.filter((o) => !o.stats);
const stats = offered.find((o) => o.stats);
check(payloads.length === 3, `all three JSON reads reached us (got ${payloads.length})`);
check(Boolean(stats), "the page reported what its reader saw");
check(stats?.stats?.json === 3, `HTML was not counted as JSON (json=${stats?.stats?.json})`);
check(stats?.stats?.sent === 3, `all three were forwarded (sent=${stats?.stats?.sent})`);

console.log(failures.length ? `\n${failures.length} failed` : "\nall good");
process.exit(failures.length ? 1 : 0);
