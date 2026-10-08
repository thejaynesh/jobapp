import assert from "node:assert/strict";
import { customHarvestSite, harvestSites, requireHarvestSite, siteForUrl } from "../../extension/sites.js";

const origin = "https://careers.new-employer.example";
const site = customHarvestSite(origin + "/openings?token=ignored");
const stored = {};
const permissions = new Set();
globalThis.chrome = {
  storage: { local: { get: async (defaults) => ({ ...defaults, ...stored }) } },
  permissions: { contains: async ({ origins }) => origins.every((value) => permissions.has(value)) },
};
assert.deepEqual(site.matches, [origin + "/*"]);
assert.equal(siteForUrl("https://other.example/jobs", [site]), null);
assert.equal(siteForUrl("https://api.careers.new-employer.example/jobs", [site]), null);
assert.throws(() => customHarvestSite("javascript:alert(1)"));
assert.throws(() => customHarvestSite("https://user:password@jobs.example"));
await assert.rejects(requireHarvestSite(origin, { requireKnown: true }), /Add job site/);
stored.customHarvestOrigins = [origin, origin, "javascript:alert(1)"];
assert.equal((await harvestSites()).filter((row) => row.id === site.id).length, 1);
await assert.rejects(requireHarvestSite(origin, { requireKnown: true }), /turned off/);
stored[site.storageKey] = true;
await assert.rejects(requireHarvestSite(origin, { requireKnown: true }), /needs its site permission/);
permissions.add(origin + "/*");
assert.equal((await requireHarvestSite(origin, { requireKnown: true })).id, site.id);
stored[site.storageKey] = false;
await assert.rejects(requireHarvestSite(origin, { requireKnown: true }), /turned off/);
console.log("Custom source permission and crawl gates passed.");
