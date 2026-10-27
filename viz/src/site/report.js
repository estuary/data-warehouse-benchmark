/**
 * The buyer's guide: where it is published, and whether to link it yet.
 *
 * No imports and no document access, so the browser tests can read it directly.
 */

/** The report's address; linked only while REPORT_LIVE is true. */
export const REPORT_URL = 'https://estuary.dev/data-warehouse-benchmark-report/';
/**
 * Whether REPORT_URL serves the 2026 report. While false, every link to it is
 * left out, so no page sends a reader to an address that does not yet carry
 * this report. Set it to true once the 2026 report is live at that address.
 */
export const REPORT_LIVE = true;

/**
 * Campaign tags on every link to the report, so the publisher's own analytics
 * can count readers the explorer sends (the explorer itself runs no analytics).
 * `utm_content` names the link: `header`, `home` or `footer`.
 */
export const REPORT_UTM = {
  utm_source: 'benchmark-explorer',
  utm_medium: 'referral',
  utm_campaign: '2026-data-warehouse-benchmark'
};

/**
 * The report's address as one link on the site carries it.
 * @param {'header' | 'home' | 'footer'} placement Which link this is.
 * @returns {string}
 */
export function reportHref(placement) {
  const url = new URL(REPORT_URL);
  for (const [key, value] of Object.entries({ ...REPORT_UTM, utm_content: placement })) url.searchParams.set(key, value);
  return url.href;
}
