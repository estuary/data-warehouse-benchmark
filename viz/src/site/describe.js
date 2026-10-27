/**
 * One sentence describing the current selection, for exported figures and for
 * the compact filter summary. A chart lifted into a deck carries this line, so
 * it must name everything a reader needs to reproduce the view.
 */
import { CLICKBENCH } from '../constants.js';

/** Data-set value -> the words a reader sees. */
export const DATASET_TEXT = {
  1: 'Join queries (TPC-DS + DSB) at about 1 GB (SF1)',
  10: 'Join queries (TPC-DS + DSB) at about 10 GB (SF10)',
  100: 'Join queries (TPC-DS + DSB) at about 100 GB (SF100)',
  1000: 'Join queries (TPC-DS + DSB) at about 1 TB (SF1000)',
  [CLICKBENCH]: 'Scan queries (ClickBench, one table of about 100 GB)'
};

/** Data-set value -> its data volume alone, for a sentence that names the chart's volume. */
export const VOLUME_TEXT = {
  1: 'about 1 GB',
  10: 'about 10 GB',
  100: 'about 100 GB',
  1000: 'about 1 TB',
  [CLICKBENCH]: 'about 100 GB (the one ClickBench table)'
};

/** `5000` -> `5 s`, `750` -> `750 ms`. */
export const fmtSla = ms => (ms >= 1000 ? `${(ms / 1000).toFixed(1).replace(/\.0$/, '')} s` : `${ms} ms`);

/**
 * @param {{dataset?: string|number, sla?: number, engines?: string[]|null, extra?: string[]}} s
 * @returns {string}
 */
export function describeSelection({ dataset, sla, engines, extra = [] }) {
  const parts = [];
  if (dataset != null) parts.push(DATASET_TEXT[dataset] ?? `Data set ${dataset}`);
  if (sla != null) parts.push(`${fmtSla(sla)} median latency target`);
  parts.push(engines && engines.length ? engines.join(', ') : 'all seven engines');
  return [...parts, ...extra].join(' · ');
}
