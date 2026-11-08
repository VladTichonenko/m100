import type { Report, TodaySummary } from './types';

/** Daily totals are authoritative server data; pending drafts are never substituted. */
export function todaySummary(report: Report): TodaySummary | null {
  return report.today ?? null;
}
