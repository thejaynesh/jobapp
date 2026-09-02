/**
 * Every site the harvest can read. The one place the list lives.
 *
 * It used to live in three: the registration in background.js, the permission
 * handling in options.js, and a hand-written checkbox in options.html. Adding a
 * site meant three edits that had to agree, and forgetting the third gave you a
 * site that was registered and permissioned with no way to turn it on.
 *
 * A row per site rather than one wildcard, and that stays deliberate: "read
 * every job board you visit" is a different thing to consent to than "read
 * LinkedIn". Each entry gets its own checkbox, its own host permission asked
 * for when you tick it, and its own revocation when you untick it.
 *
 * -------------------------------------------------------------------------
 * Adding a site
 * -------------------------------------------------------------------------
 * Append a row here and reload the extension. Nothing else in the extension
 * needs to change — no parser, no selectors, no endpoint names. The reader on
 * the server walks whatever JSON arrives looking for objects with a title, a
 * company and an id, so a site it has never seen usually works on the first
 * try.
 *
 * On the server, add the host to `HARVEST_SOURCES` in app/services/harvest.py
 * so its yield is counted under its own name instead of LinkedIn's. If the
 * site's payload names a field something nobody else does, add that name to
 * the matching alias tuple in the same file. See docs/HARVEST.md.
 *
 * Fields:
 *   id         short slug, used to build content-script ids
 *   label      what the options page calls it
 *   storageKey where the toggle is stored. LinkedIn keeps the original
 *              `harvest` key so existing installs need no migration; every
 *              other site is `harvest<Id>`.
 *   matches    host patterns — the content-script match *and* the permission
 *   note       optional, shown under the checkbox
 */
export const HARVEST_SITES = [
  {
    id: "linkedin",
    label: "LinkedIn",
    storageKey: "harvest",
    matches: ["https://www.linkedin.com/*"],
    note:
      "Full descriptions and salary bands the guest API never returns. " +
      "LinkedIn answers the server with a challenge, so for those postings " +
      "this is the only way descriptions arrive at all.",
  },
  {
    id: "indeed",
    label: "Indeed",
    storageKey: "harvestIndeed",
    matches: ["https://*.indeed.com/*"],
    note: "Answers a datacenter IP with a challenge.",
  },
  {
    id: "glassdoor",
    label: "Glassdoor",
    storageKey: "harvestGlassdoor",
    matches: ["https://*.glassdoor.com/*"],
    note: "Answers a datacenter IP with a challenge.",
  },
  {
    id: "workday",
    label: "Workday career sites",
    storageKey: "harvestWorkday",
    matches: ["https://*.myworkdayjobs.com/*"],
    note:
      "Per employer rather than a board, so browsing one is the only way its " +
      "postings are seen at all.",
  },
  {
    id: "dice",
    label: "Dice",
    storageKey: "harvestDice",
    matches: ["https://*.dice.com/*"],
  },
  {
    id: "ziprecruiter",
    label: "ZipRecruiter",
    storageKey: "harvestZipRecruiter",
    matches: ["https://*.ziprecruiter.com/*"],
  },
  {
    id: "wellfound",
    label: "Wellfound",
    storageKey: "harvestWellfound",
    matches: ["https://wellfound.com/*"],
  },
  {
    id: "builtin",
    label: "Built In",
    storageKey: "harvestBuiltIn",
    matches: ["https://*.builtin.com/*"],
  },
  {
    id: "simplyhired",
    label: "SimplyHired",
    storageKey: "harvestSimplyHired",
    matches: ["https://*.simplyhired.com/*"],
  },
  {
    id: "monster",
    label: "Monster",
    storageKey: "harvestMonster",
    matches: ["https://*.monster.com/*"],
  },
  {
    id: "otta",
    label: "Otta / Welcome to the Jungle",
    storageKey: "harvestOtta",
    matches: ["https://otta.com/*", "https://*.welcometothejungle.com/*"],
  },
  {
    id: "jobright",
    label: "JobRight",
    storageKey: "harvestJobRight",
    matches: ["https://*.jobright.ai/*", "https://jobright.ai/*"],
    note:
      "Its board is behind a login and has no public API, so browsing is the " +
      "only way its listings are seen at all.",
  },
  {
    id: "tsenta",
    label: "Tsenta",
    storageKey: "harvestTsenta",
    // The board lives on `dashboard.`, and the bare domain is where sign-in
    // and the marketing pages are. Both are matched so a redirect through
    // either one still lands somewhere the reader is registered.
    matches: ["https://*.tsenta.com/*", "https://tsenta.com/*"],
    note:
      "An aggregator like JobRight, reading from a wider set of career pages " +
      "and boards. Login-only, and it will not show a full list until its " +
      "search is submitted — the crawl does that for you.",
  },
  {
    id: "handshake",
    label: "Handshake",
    storageKey: "harvestHandshake",
    matches: ["https://*.joinhandshake.com/*"],
    note: "Also login-only.",
  },
  {
    id: "hiringcafe",
    label: "Hiring Cafe",
    storageKey: "harvestHiringCafe",
    // Both spellings. `hiring.cafe` is the address you type and the one the
    // crawl queues; it redirects to `hiringcafe.com`, which is the page that
    // actually loads — so matching only the first registered the reader on a
    // URL that never renders. The board opened, scrolled, and forwarded
    // nothing, for exactly as long as nobody looked.
    matches: [
      "https://hiring.cafe/*",
      "https://hiringcafe.com/*",
      "https://*.hiringcafe.com/*",
    ],
  },
  {
    id: "greenhouse",
    label: "Greenhouse (all companies)",
    storageKey: "harvestGreenhouse",
    matches: ["https://my.greenhouse.io/*"],
    note:
      "Greenhouse's own job-seeker board — every company on the platform, not " +
      "one. Login-only, so the server cannot see it at all. Worth more than " +
      "the postings: each one names a company board the fetcher can then read " +
      "by API forever.",
  },
  {
    id: "amazon",
    label: "Amazon Jobs",
    storageKey: "harvestAmazon",
    matches: ["https://*.amazon.jobs/*", "https://amazon.jobs/*"],
    note: "Its own board with no public API, like LinkedIn.",
  },
  {
    id: "google",
    label: "Google Careers",
    storageKey: "harvestGoogle",
    // Scoped to the careers path rather than google.com: "read everything you
    // do on Google" is not the permission this needs, and asking for it would
    // be the single most alarming line in the install prompt.
    matches: ["https://www.google.com/about/careers/*"],
  },
];

/**
 * Whether a Chrome match pattern covers a URL.
 *
 * Only the shapes this file actually uses: an exact host, a `*.` host
 * wildcard, and a trailing path wildcard. Chrome applies the real patterns to
 * content scripts and permissions; this is for deciding, in our own code,
 * which site a URL belongs to.
 */
function patternMatches(pattern, url) {
  const parts = /^(\*|https?):\/\/([^/]+)(\/.*)$/.exec(pattern);
  if (!parts) return false;
  const [, scheme, hostPart, pathPart] = parts;

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (scheme !== "*" && parsed.protocol !== `${scheme}:`) return false;

  if (hostPart.startsWith("*.")) {
    const base = hostPart.slice(2);
    // Chrome's `*.example.com` covers the bare domain too.
    if (parsed.hostname !== base && !parsed.hostname.endsWith(`.${base}`)) {
      return false;
    }
  } else if (parsed.hostname !== hostPart) {
    return false;
  }

  const prefix = pathPart.endsWith("*") ? pathPart.slice(0, -1) : pathPart;
  return parsed.pathname.startsWith(prefix);
}

/** The site a URL belongs to, or null if it is not one we know. */
export function siteForUrl(url) {
  return (
    HARVEST_SITES.find((site) =>
      site.matches.some((pattern) => patternMatches(pattern, url)),
    ) || null
  );
}
