import type { Line, Position, Report } from './types';

export function cardFigures(position: Position, report: Report, lines: Line[]) {
  const quantity = lines.filter(line => line.position_id === position.id).reduce((total, line) => {
    const value = Number((line.quantity || '').replace(',', '.'));
    return total + (Number.isFinite(value) && value >= 0 ? value : 0);
  }, 0);
  const delta = report.draft.status === 'done' || report.draft.status === 'cancelled'
    ? 0 : quantity - (report.draft.mode === 'replace' ? Number(report.draft.before[position.id] || 0) : 0);
  // A partial operation may already have updated Sheets. Never preview its volume twice.
  const applied = report.operation?.sheets_done;
  const pendingDelta = applied ? 0 : delta;
  const completed = Number(position.completed) + pendingDelta;
  const rate = position.unit_pay == null ? null : Number(position.unit_pay);
  return {
    completed,
    remaining: Number(position.planned) - completed,
    plannedPay: position.planned_pay == null
      ? rate == null ? null : Number(position.planned) * rate
      : Number(position.planned_pay),
    completedPay: rate == null
      ? pendingDelta === 0 && position.completed_pay != null ? Number(position.completed_pay) : null
      : completed * rate,
  };
}
