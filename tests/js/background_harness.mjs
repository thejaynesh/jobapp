// Test support for tests/test_extension_overlay_hosts.py. Node, not a browser:
// the extension's service worker is imported with `chrome` stubbed.
// Load background.js under a stub `chrome`, seeded from argv[2] (JSON state),
// wait for its startup sync, and print the overlay registration it leaves.
const state = JSON.parse(process.argv[2]);
const registered = new Map((state.registered || []).map((s) => [s.id, s]));
const granted = new Set(state.granted || []);

function anything() {
  // Every unknown API: callable, awaitable, and any property is another stub.
  const fn = () => Promise.resolve(undefined);
  return new Proxy(fn, {
    get: (target, key) => (key === "then" ? undefined : anything()),
    apply: () => Promise.resolve(undefined),
  });
}

const overrides = {
  storage: { local: {
    get: async (defaults) => ({ ...(typeof defaults === "object" ? defaults : {}), ...(state.stored || {}) }),
    set: async () => {},
  } },
  permissions: {
    contains: async ({ origins }) => origins.every((o) => granted.has(o) || granted.has("https://*/*")),
    onAdded: { addListener() {} }, onRemoved: { addListener() {} },
  },
  scripting: {
    getRegisteredContentScripts: async ({ ids } = {}) =>
      [...registered.values()].filter((s) => !ids || ids.includes(s.id)),
    registerContentScripts: async (scripts) => scripts.forEach((s) => registered.set(s.id, s)),
    unregisterContentScripts: async ({ ids } = {}) => (ids || []).forEach((id) => registered.delete(id)),
  },
};

function build(base, path) {
  return new Proxy(base, {
    get(target, key) {
      if (key in target) {
        const value = target[key];
        return value && typeof value === "object" && !Array.isArray(value) ? build(value, [...path, key]) : value;
      }
      return anything();
    },
  });
}
globalThis.chrome = build(overrides, []);
globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}), text: async () => "" });

await import(process.argv[3]);
await new Promise((resolve) => setTimeout(resolve, 200));
console.log(JSON.stringify(registered.get("jobapp-overlay") || null));
process.exit(0);
