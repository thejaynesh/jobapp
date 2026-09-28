/**
 * Where the panel draws: the one place the list lives, for the service worker
 * (which registers the content scripts) and the options page (which asks for
 * the permission).
 *
 * Two lists, granted separately, and that stays deliberate.
 *
 * `OVERLAY_CORE` is exactly what earlier versions asked for, and must not
 * change. The panel runs only where the user granted access, and the
 * registration checks that grant; adding a host to this list would make every
 * existing install fail the check on update and quietly lose the panel until
 * someone found the options page and re-ticked it.
 *
 * `OVERLAY_MORE` is the rest of the application systems the fill works on —
 * autograph (github.com/nikhil-ghind/autograph) fills 21 of them — asked for by
 * its own checkbox. Declining it costs the core list nothing.
 */

export const OVERLAY_CORE = [
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

export const OVERLAY_MORE = [
  // Large employers
  "https://*.icims.com/*",
  "https://*.taleo.net/*",
  "https://*.oraclecloud.com/hcmUI/*",
  "https://*.successfactors.com/*",
  "https://*.successfactors.eu/*",
  "https://*.eightfold.ai/*",
  "https://*.avature.net/*",
  "https://recruiting.ultipro.com/*",
  "https://recruiting2.ultipro.com/*",
  "https://jobs.dayforcehcm.com/*",
  // Small and mid-sized employers
  "https://recruiting.paylocity.com/*",
  "https://*.bamboohr.com/*",
  "https://jobs.jobvite.com/*",
  "https://*.applytojob.com/*",
  "https://*.breezy.hr/*",
  "https://ats.rippling.com/*",
  "https://*.pinpointhq.com/*",
  "https://*.teamtailor.com/*",
  "https://*.jobs.personio.de/*",
  "https://*.jobs.personio.com/*",
  "https://jobs.gem.com/*",
];

/** The overlay's content-script files, in order: autofill.js defines what overlay.js calls. */
export const OVERLAY_FILES = ["autofill.js", "overlay.js"];

/**
 * Where to register the panel, or null for nowhere.
 *
 * `overlay` and `more` are the two checkboxes; `hasCore` and `hasMore` whether
 * their permissions are actually held — a ticked box with the permission
 * revoked out from under it registers nothing there.
 */
export function overlayMatches({ overlay, more, hasCore, hasMore }) {
  if (!overlay || !hasCore) return null;
  return more && hasMore ? [...OVERLAY_CORE, ...OVERLAY_MORE] : [...OVERLAY_CORE];
}
