/**
 * Hands a board's own credential to the server, so it can sweep without us.
 *
 * `tsenta.js` reads the board's API from inside the tab, which works and which
 * only happens while a browser is open on it. The server can do the same thing
 * on a schedule if it has the durable half of the credential: Firebase's web
 * SDK keeps an ID token that lasts an hour and a refresh token that lasts until
 * it is revoked, and Google's public `securetoken` endpoint mints fresh ID
 * tokens from the second one.
 *
 * **Isolated world, and that is the whole reason this is a separate file.**
 * Everything else the extension reads off a page travels through
 * `window.postMessage`, which is a public channel — every script on the page
 * can listen to it, including the analytics and error-reporting bundles a
 * modern site loads from third parties. A job listing going past those is
 * nothing; a refresh token going past them is the user's account. An isolated
 * content script shares the page's *origin* for storage purposes but not its
 * globals, so it can open the same IndexedDB the site's own SDK wrote and call
 * `chrome.runtime.sendMessage` directly. The token never enters the page's
 * message channel at all.
 *
 * Nothing here is scraped out of internals nobody offered: this is the same
 * storage the site's own SDK reads on every page load, in the browser profile
 * the user is signed into, sent to the user's own server.
 *
 * Sent on every visit rather than once. A credential that has gone stale is
 * then repaired by opening the board — which is a thing people do anyway, and
 * means nobody has to learn that it is the fix.
 */

const SITE = "tsenta";

// One link per session is plenty: the refresh token does not change between
// page views, and re-posting it on every SPA route change would be noise.
const SENT_KEY = "jobapp:linked:tsenta";

/**
 * Every Firebase auth record this origin has stored, whatever it called them.
 *
 * Searched rather than addressed. The documented layout is a
 * `firebaseLocalStorageDb` database holding a `firebaseLocalStorage` store
 * keyed `firebase:authUser:<apiKey>:<appName>` — but the app name is the
 * site's choice, the SDK has moved this before, and a lookup written against
 * one exact key fails silently the day any of that changes. Walking the stores
 * and keeping anything with a refresh token in it costs a few milliseconds and
 * survives a rename.
 */
async function findCredential() {
  let databases = [];
  try {
    databases = (await indexedDB.databases()) || [];
  } catch (_) {
    return null;
  }

  const open = (name) =>
    new Promise((resolve) => {
      let request;
      try {
        request = indexedDB.open(name);
      } catch (_) {
        return resolve(null);
      }
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => resolve(null);
      request.onblocked = () => resolve(null);
    });

  const readAll = (db, store) =>
    new Promise((resolve) => {
      try {
        const query = db.transaction(store, "readonly").objectStore(store).getAll();
        query.onsuccess = () => resolve(query.result || []);
        query.onerror = () => resolve([]);
      } catch (_) {
        resolve([]);
      }
    });

  for (const entry of databases) {
    const name = entry && entry.name;
    if (!name || !/firebase/i.test(name)) continue;
    const db = await open(name);
    if (!db) continue;
    try {
      for (const store of Array.from(db.objectStoreNames)) {
        for (const row of await readAll(db, store)) {
          const value = (row && row.value) || row;
          const manager = value && value.stsTokenManager;
          const refreshToken = manager && manager.refreshToken;
          const apiKey = value && value.apiKey;
          if (typeof refreshToken === "string" && refreshToken &&
              typeof apiKey === "string" && apiKey) {
            return { apiKey, refreshToken };
          }
        }
      }
    } finally {
      try {
        db.close();
      } catch (_) {
        /* already gone */
      }
    }
  }
  return null;
}

/**
 * The same thing out of localStorage, for a site persisting auth there.
 *
 * Firebase supports three persistences and picks one at initialisation. The
 * default is IndexedDB, but `browserSessionPersistence` and the older
 * `browserLocalPersistence` both land here — and a site that switches would
 * otherwise look exactly like a site that signed the user out.
 */
function findInLocalStorage() {
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (!key || !/^firebase:authUser:/.test(key)) continue;
      const value = JSON.parse(localStorage.getItem(key) || "null");
      const refreshToken = value && value.stsTokenManager &&
        value.stsTokenManager.refreshToken;
      if (typeof refreshToken === "string" && refreshToken && value.apiKey) {
        return { apiKey: String(value.apiKey), refreshToken };
      }
    }
  } catch (_) {
    /* blocked storage, or something in it is not JSON */
  }
  return null;
}

function connected() {
  try {
    return Boolean(chrome.runtime && chrome.runtime.id);
  } catch (_) {
    return false;
  }
}

async function link() {
  if (!connected()) return;
  try {
    if (sessionStorage.getItem(SENT_KEY)) return;
  } catch (_) {
    /* private mode: one extra post is the whole cost */
  }

  const found = (await findCredential()) || findInLocalStorage();
  // Signed out, or a marketing page that never initialised the SDK. Both are
  // normal and neither is worth reporting: the server already knows when it
  // was last linked, which is the number that actually answers "why did the
  // scheduled sweep stop".
  if (!found) return;

  try {
    chrome.runtime.sendMessage(
      {
        type: "link-account",
        site: SITE,
        apiKey: found.apiKey,
        refreshToken: found.refreshToken,
      },
      () => {
        void chrome.runtime.lastError;
        try {
          sessionStorage.setItem(SENT_KEY, "1");
        } catch (_) {
          /* as above */
        }
      },
    );
  } catch (_) {
    /* orphaned by a reload between the check and the call */
  }
}

// After load rather than at document_start: the SDK writes its record once
// Firebase has resolved the signed-in user, which is a network round trip away.
// Retried a few times because that round trip is not instant and a single look
// would routinely land before it.
let attempts = 0;
const timer = setInterval(() => {
  attempts += 1;
  if (attempts > 10) clearInterval(timer);
  link();
}, 3000);
link();
