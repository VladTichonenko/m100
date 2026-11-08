import React, { useEffect, useState } from 'react';
import { ArrowUpRight, Check, ChevronRight, Clock3, Cloud, Layers3, ListChecks, X } from 'lucide-react';
import { todaySummary } from './dashboard';
import { SheetFeature } from './SheetFeature';
import { Drawer } from './Drawer';
import type { Report } from './types';

const format = (value: string | number) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(value));

function LiveClock({ timezone }: { timezone: string }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const timer = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(timer); }, []);
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: timezone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(now);
  const part = (type: string) => Number(parts.find(p => p.type === type)?.value || 0);
  const hour = part('hour'), minute = part('minute'), second = part('second');
  return <article className="dashboard-clock dashboard-tile">
    <div className="utility-card-top"><span><Clock3 size={15} />Сейчас</span><span className="live-mark" title="Время обновляется каждую секунду" /></div>
    <div className="clock-face" aria-hidden="true">
      {Array.from({ length: 12 }, (_, index) => <i key={index} style={{ transform: `rotate(${index * 30}deg)` }} />)}
      <b className="clock-hour" style={{ transform: `rotate(${(hour % 12) * 30 + minute / 2}deg)` }} />
      <b className="clock-minute" style={{ transform: `rotate(${minute * 6 + second / 10}deg)` }} />
      <b className="clock-second" style={{ transform: `rotate(${second * 6}deg)` }} /><b className="clock-pin" />
    </div>
    <time dateTime={now.toISOString()} className="clock-digital">{String(hour).padStart(2, '0')}<span>:</span>{String(minute).padStart(2, '0')}</time>
    <span className="clock-location">{timezone === 'Europe/Minsk' ? 'Минск · GMT+3' : timezone}</span>
  </article>;
}

export function DesktopDashboard({ report }: { report: Report }) {
  const [showToday, setShowToday] = useState(false);
  const today = todaySummary(report);
  const bitrixSaved = report.operation?.bitrix_done && !report.operation?.bitrix_skipped;

  return <aside className="desktop-dashboard" aria-label="Мой рабочий день">
    <div className="dashboard-section-heading"><span>ПОД РУКОЙ</span><span>Ваш рабочий день <i /></span></div>
    <SheetFeature tableUrl={report.table_url} />

    <article className="bitrix-feature dashboard-tile">
      <div className="bitrix-copy"><span className="feature-eyebrow"><Cloud size={17} />СВЯЗЬ С КОМАНДОЙ</span><h2>Битрикс<span>24</span><Clock3 size={18} /></h2>
        <p>{report.bitrix_enabled ? 'Дневной отчёт для всей команды.' : 'Отчёт будет рядом\nс задачами команды.'}</p>
        <span className={`integration-status ${report.operation?.sheets_done && !bitrixSaved ? 'is-pending' : report.bitrix_enabled ? 'is-connected' : ''}`}><i />{!report.bitrix_enabled ? 'Пока не подключён' : bitrixSaved ? 'Отчёт синхронизирован' : report.operation?.sheets_done ? 'Ожидает повторной отправки' : 'Подключён'}</span>
        {report.bitrix_enabled && report.bitrix_url && <a className="bitrix-portal-link" href={report.bitrix_url} target="_blank" rel="noreferrer">Открыть Битрикс24 <ArrowUpRight size={14} /></a>}
      </div>
      <div className="task-art" aria-hidden="true"><div className="task-paper task-paper-back"><span /><span /><span /></div><div className="task-paper task-paper-front"><div className="task-paper-top"><span className="task-avatar"><Layers3 size={13} /></span><b>Дневной отчёт</b><span>•••</span></div><div className="task-paper-line" /><div className="task-paper-line short" /><span className="task-chip task-chip-blue"><Check size={11} />Объёмы работ</span><span className="task-chip task-chip-coral"><ListChecks size={11} />Всё по плану</span><div className="task-paper-footer"><span className="task-dot" /><span className="task-dot" /><span className="task-dot" /><i /></div></div></div>
    </article>

    <div className="dashboard-utilities"><LiveClock timezone={report.timezone || 'Europe/Minsk'} />
      <button className="today-feature dashboard-tile" onClick={() => setShowToday(true)} aria-label="Посмотреть все мои работы за сегодня">
        <span className="utility-card-top"><span><ListChecks size={15} />Мои работы</span><ArrowUpRight size={17} /></span>
        <span className="today-illustration" aria-hidden="true"><span /><span /><span /><i><Check size={17} /></i></span>
        <span className="today-total">{today ? String(today.lines.length).padStart(2, '0') : '—'}<small>позиций за сегодня</small></span>
        <span className="today-view-link">{today?.lines.length ? 'Посмотреть работы' : 'Открыть сводку'}<ChevronRight size={14} /></span>
      </button>
    </div>
    <p className="dashboard-footnote">В сводке — сохранённые в таблице работы за сегодня</p>

    <Drawer open={showToday} onClose={() => setShowToday(false)} className="today-dialog position-dialog" label="Все мои работы за сегодня">
      <div className="picker-heading"><span className="voice-block-icon"><ListChecks size={22} /></span><div className="picker-heading-copy"><h2>Мои работы за сегодня</h2><small>{today?.day ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long' }).format(new Date(today.day + 'T12:00:00')) : 'Данные недоступны'}</small></div><button className="close-button" aria-label="Закрыть сводку дня" onClick={() => setShowToday(false)}><X size={20} /></button></div>
      <p className="today-dialog-note">Сохранённые объёмы из таблицы. Текущий черновик попадёт сюда после сохранения.</p>
      <div className="today-work-list">{today?.lines.length ? today.lines.map(line => <div className="today-work-row" key={line.position_id}><div><small>{line.position_id}</small><span>{line.name}</span></div><strong>{format(line.quantity)} <small>{line.unit}</small></strong></div>) : <div className="today-empty"><ListChecks size={28} /><p>{today ? 'За сегодня пока нет сохранённых работ.' : 'Обновите страницу, чтобы загрузить сводку дня.'}</p></div>}</div>
      {today?.lines.length && today.total_pay != null ? <div className="today-pay"><span>На руки за сегодня</span><strong>{format(today.total_pay)}</strong></div> : null}
    </Drawer>
  </aside>;
}
