import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AlertTriangle, ArrowUpRight, Check, ChevronDown, CircleCheck, ClipboardList, LoaderCircle, Mic, PencilLine, Plus, RotateCcw, Save, Search, Send, Trash2, X } from 'lucide-react';
import { cardFigures } from './figures';
import { likelyPositions } from './positions';
import { DesktopDashboard } from './DesktopDashboard';
import { SheetFeature } from './SheetFeature';
import { Drawer } from './Drawer';
import type { CardDetails, Line, Position, Report } from './types';
import './style.css';

type TelegramApp = {
  initData: string; ready(): void; expand(): void; close(): void;
  setHeaderColor(color: string): void; setBackgroundColor(color: string): void;
  HapticFeedback?: { notificationOccurred(type: 'success'): void };
};
const telegram = (window as Window & { Telegram?: { WebApp?: TelegramApp } }).Telegram?.WebApp;

const reportId = location.pathname.match(/^\/report\/([a-zA-Z0-9_-]+)$/)?.[1];
const tokenKey = `m100-token:${reportId}`;
let token = '';
try {
  token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem(tokenKey) || '';
  if (token) sessionStorage.setItem(tokenKey, token);
} catch { /* A fragment token still permits access when storage is unavailable. */ }
if (token && location.hash) history.replaceState(null, '', location.pathname + location.search);

const format = (value: unknown) => value == null || value === '' ? '—' : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(value));
const numeric = (value: string) => value.trim().replace(',', '.');
type Form = Record<keyof CardDetails | 'quantity', string>;
const workFilters = ['Все', 'Распознано', 'Уточнить', 'Сохранено'] as const;
type WorkFilter = typeof workFilters[number];
const detailFields: { key: keyof CardDetails; label: string }[] = [
  { key: 'unit', label: 'ед. изм.' },
  { key: 'planned', label: 'Объём по смете' },
  { key: 'unit_pay', label: 'Ставка за единицу' },
  { key: 'planned_pay', label: 'Оплата по смете' },
  { key: 'remaining', label: 'Остаток объёма' },
  { key: 'completed_pay', label: 'Оплата за всё время' },
];

function App() {
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(Boolean(reportId));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [formPosition, setFormPosition] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [toast, setToast] = useState('');
  const [deleted, setDeleted] = useState<{ line: Line; index: number } | null>(null);
  const [deleteIndex, setDeleteIndex] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState('');
  const [picker, setPicker] = useState(false);
  const [search, setSearch] = useState('');
  const [workSearch, setWorkSearch] = useState('');
  const [workFilter, setWorkFilter] = useState<WorkFilter>('Все');
  const [allPositions, setAllPositions] = useState(false);
  const [emptyPicker, setEmptyPicker] = useState(false);
  const [returnToVoice, setReturnToVoice] = useState(false);
  const [rewriting, setRewriting] = useState<'request' | 'closing' | null>(null);
  const [profilePhoto, setProfilePhoto] = useState('');
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [overrunReview, setOverrunReview] = useState(false);
  const volumeInputRef = useRef<HTMLInputElement>(null);
  const deleteDialogRef = useRef<HTMLDialogElement>(null);
  const cardPress = useRef<{ timer: number; pointerId: number; x: number; y: number } | null>(null);
  const pressConsumed = useRef(false);
  const triggerRef = useRef<HTMLElement | null>(null);
  const localKey = `m100-card:${reportId}`;

  const lines = report?.draft.lines || [];
  const positions = new Map(report?.catalog.map(position => [position.id, position]));
  const saved = report?.draft.status === 'done';
  const retrying = report?.draft.status === 'saving';
  const cancelled = report?.draft.status === 'cancelled';
  const locked = Boolean(saved || retrying || cancelled || busy);
  const visible = lines.map((line, index) => ({ line, index })).filter(({ line }) => !line.deleted);
  const filteredWorks = visible.filter(({ line }) => {
    const position = positions.get(line.position_id || '');
    const text = `${position?.name ?? ''} ${line.phrase} ${line.position_id ?? ''}`.toLocaleLowerCase();
    return text.includes(workSearch.trim().toLocaleLowerCase()) && (workFilter === 'Все' || workStatus(line) === workFilter);
  });
  const overruns = report && !saved && !retrying && !cancelled ? report.catalog.flatMap(position => {
    const figures = cardFigures(position, report, lines);
    const baseline = Number(position.completed);
    return figures.remaining < 0 && figures.completed > baseline
      ? [{ id: position.id, name: position.name, excess: -figures.remaining, unit: position.unit }] : [];
  }) : [];
  const selectedLine = selected == null ? null : lines[selected];
  const profile = report?.profile;
  const profileInitials = profile?.name.split(/\s+/).slice(0, 2).map(word => word[0]).join('').toLocaleUpperCase() || 'TG';
  const suggested = likelyPositions(report?.catalog || [], selectedLine, formPosition);
  const pickerPositions = (allPositions ? report?.catalog || [] : suggested).filter(position =>
    `${position.name} ${position.id}`.toLocaleLowerCase().includes(search.toLocaleLowerCase().trim()));

  function openPicker(forNew = false) {
    volumeInputRef.current?.blur();
    setSearch(''); setEmptyPicker(forNew); setAllPositions(forNew || Boolean(formPosition)); setPicker(true);
  }

  function accept(data: Report) { setReport(data); }

  function load(data: Report) {
    accept(data);
    try {
      const cached = JSON.parse(sessionStorage.getItem(localKey) || 'null');
      if (data.draft.status === 'draft' && cached?.revision === data.draft.revision
        && data.draft.lines[cached.index] && cached.form) {
        const line = data.draft.lines[cached.index];
        const position = data.catalog.find(item => item.id === cached.position);
        const quantity = String(cached.form.quantity ?? line.quantity ?? '');
        const previewLines = data.draft.lines.map((item, index) => index === cached.index ? { ...item, quantity } : item);
        const figures = position ? cardFigures(position, data, previewLines) : null;
        setSelected(cached.index); setForm({
          quantity, name: position?.name ?? line.phrase, unit: position?.unit ?? '',
          planned: position?.planned ?? '', unit_pay: position?.unit_pay ?? '',
          planned_pay: String(figures?.plannedPay ?? ''), remaining: String(figures?.remaining ?? ''),
          completed_pay: String(figures?.completedPay ?? ''),
        });
        setFormPosition(cached.position); setDirty(true);
      }
    } catch { /* Browser storage is optional. */ }
  }

  async function request(action = '', method = 'GET', body?: object) {
    const response = await fetch(`/api/reports/${reportId}${action}`, {
      method, headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json();
    if (!response.ok) {
      if (data.draft) accept(data);
      throw new Error(data.error || 'Не удалось выполнить запрос');
    }
    return data as Report;
  }

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 1000px)');
    const resetFilters = () => { if (desktop.matches) { setWorkSearch(''); setWorkFilter('Все'); setTranscriptOpen(false); } };
    desktop.addEventListener('change', resetFilters);
    return () => desktop.removeEventListener('change', resetFilters);
  }, []);

  useEffect(() => {
    if (telegram?.initData) {
      telegram.ready(); telegram.expand();
      telegram.setHeaderColor('#f7f7f8'); telegram.setBackgroundColor('#f7f7f8');
    }
    if (reportId) {
      request().then(load).catch(e => setError(e.message || 'Сервер недоступен')).finally(() => setLoading(false));
    }
  }, []);

  useEffect(() => {
    if (!report) return;
    try {
      if (dirty && form && selected != null) sessionStorage.setItem(localKey, JSON.stringify({
        revision: report.draft.revision, index: selected, form, position: formPosition,
      }));
      else sessionStorage.removeItem(localKey);
    } catch { /* Editing still works without local storage. */ }
  }, [dirty, form, selected, formPosition, report, localKey]);

  useEffect(() => {
    if (!profile?.has_photo || !reportId) { setProfilePhoto(''); return; }
    const controller = new AbortController();
    let objectUrl = '';
    fetch(`/api/reports/${reportId}/avatar`, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error('Фото недоступно'); return response.blob(); })
      .then(blob => { if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(blob); setProfilePhoto(objectUrl); } })
      .catch(() => { if (!controller.signal.aborted) setProfilePhoto(''); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [profile?.has_photo]);

  useEffect(() => {
    if (selected != null && !picker && !locked && window.matchMedia('(min-width: 1000px)').matches) {
      volumeInputRef.current?.focus();
      volumeInputRef.current?.select();
    }
  }, [selected, picker, locked]);

  useEffect(() => {
    if (deleteIndex !== null) {
      if (!deleteDialogRef.current?.open) deleteDialogRef.current?.showModal();
    } else if (deleteDialogRef.current?.open) deleteDialogRef.current.close();
  }, [deleteIndex]);
  useEffect(() => {
    window.addEventListener('blur', cancelCardPress);
    window.addEventListener('scroll', cancelCardPress, true);
    return () => {
      cancelCardPress();
      window.removeEventListener('blur', cancelCardPress);
      window.removeEventListener('scroll', cancelCardPress, true);
    };
  }, []);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  useEffect(() => {
    if (!toast) return;
    const timeout = setTimeout(() => { setToast(''); setDeleted(null); }, 6500);
    return () => clearTimeout(timeout);
  }, [toast]);

  function effectivePosition(line: Line): Position | undefined {
    const position = positions.get(line.position_id || '');
    if (!position) return;
    return position;
  }

  function workStatus(line: Line): Exclude<WorkFilter, 'Все'> {
    if (saved) return 'Сохранено';
    return !positions.has(line.position_id || '') || line.quantity == null || line.question ? 'Уточнить' : 'Распознано';
  }

  function resetWorkFilters() { setWorkSearch(''); setWorkFilter('Все'); }

  function openCard(index: number) {
    const line = lines[index];
    const position = effectivePosition(line);
    if (!report) return;
    const figures = position ? cardFigures(position, report, lines) : null;
    const values = {
      name: position?.name ?? line.phrase,
      unit: position?.unit ?? line.unit ?? '',
      planned: position?.planned ?? '',
      unit_pay: position?.unit_pay ?? '',
      planned_pay: figures?.plannedPay ?? '',
      remaining: figures?.remaining ?? '',
      completed_pay: figures?.completedPay ?? '',
      quantity: line.quantity ?? '',
    };
    triggerRef.current = document.activeElement as HTMLElement;
    setForm(Object.fromEntries(Object.entries(values).map(([key, value]) => [key, String(value)])) as Form);
    setFormPosition(line.position_id); setSelected(index); setDirty(false); setError('');
  }

  function closeCard() {
    if (busy) return;
    setSelected(null); setForm(null); setDirty(false); setError('');
    triggerRef.current?.focus();
  }

  function editField(key: keyof Form, value: string) {
    if (!form || locked) return;
    const next = { ...form, [key]: value };
    if (key === 'quantity' && report && selectedLine) {
      const position = positions.get(formPosition || '');
      if (position && next.quantity.trim() !== '' && Number.isFinite(Number(numeric(next.quantity)))) {
        const nextLines = lines.map((line, index) => index === selected ? { ...line, position_id: formPosition, quantity: numeric(next.quantity) } : line);
        const figures = cardFigures(position, report, nextLines);
        next.planned_pay = String(figures.plannedPay ?? '');
        next.remaining = String(figures.remaining);
        next.completed_pay = String(figures.completedPay ?? '');
      }
    }
    setForm(next); setDirty(true); setError('');
  }

  function editBody(line: Line) {
    return { position_id: line.position_id, quantity: line.quantity, phrase: line.phrase, deleted: Boolean(line.deleted) };
  }

  async function saveCard() {
    if (!report || !form || selected == null || busy) return;
    const quantity = numeric(form.quantity);
    if (!formPosition || !quantity || !Number.isFinite(Number(quantity)) || Number(quantity) < 0) {
      setError('Выберите работу и укажите неотрицательный выполненный объём.'); return;
    }
    const line = { ...lines[selected], position_id: formPosition, quantity, deleted: false };
    setBusy(true); setError('');
    try {
      const data = await request(`/lines/${selected}`, 'PATCH', { revision: report.draft.revision, line: editBody(line) });
      accept(data); setSelected(null); setForm(null); setDirty(false); setToast('Изменения применены к черновику');
    } catch (e) { setError(e instanceof Error ? e.message : 'Не удалось применить изменения'); }
    finally { setBusy(false); }
  }

  function cancelCardPress() {
    if (cardPress.current) window.clearTimeout(cardPress.current.timer);
    cardPress.current = null;
  }

  function requestDelete(index: number) {
    if (locked || !lines[index] || lines[index].deleted) return;
    setDeleteError(''); setDeleteIndex(index);
  }

  function startCardPress(event: React.PointerEvent<HTMLButtonElement>, index: number) {
    cancelCardPress();
    pressConsumed.current = false;
    if (locked || !event.isPrimary || event.button !== 0) return;
    cardPress.current = {
      pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      timer: window.setTimeout(() => {
        cardPress.current = null;
        pressConsumed.current = true;
        requestDelete(index);
      }, 550),
    };
  }

  function moveCardPress(event: React.PointerEvent<HTMLButtonElement>) {
    const press = cardPress.current;
    if (press && (event.pointerId !== press.pointerId || Math.hypot(event.clientX - press.x, event.clientY - press.y) > 10)) cancelCardPress();
  }

  function closeDelete() {
    if (busy) return;
    setDeleteIndex(null); setDeleteError('');
  }

  async function deleteCard() {
    if (!report || deleteIndex == null || locked || !lines[deleteIndex]) return;
    const index = deleteIndex;
    const line = lines[index];
    setBusy(true); setError(''); setDeleteError('');
    try {
      const data = await request(`/lines/${index}`, 'DELETE', { revision: report.draft.revision });
      accept(data); setDeleteIndex(null); setSelected(null); setForm(null); setDirty(false); setDeleted({ line, index }); setToast('Работа удалена из черновика');
    } catch (e) { setDeleteError(e instanceof Error ? e.message : 'Не удалось удалить карточку'); }
    finally { setBusy(false); }
  }

  async function undoDeleted() {
    if (!report || !deleted || busy) return;
    const next = [...lines];
    if (next[deleted.index]?.deleted) next[deleted.index] = deleted.line;
    else next.splice(deleted.index, 0, deleted.line);
    setBusy(true);
    try {
      accept(await request('', 'PUT', { revision: report.draft.revision, lines: next.map(editBody) }));
      setDeleted(null); setToast('Работа возвращена в черновик');
    } catch (e) { setError(e instanceof Error ? e.message : 'Не удалось вернуть карточку'); }
    finally { setBusy(false); }
  }

  async function overwriteDraft() {
    if (!report || locked) return;
    setBusy(true); setRewriting('request'); setError('');
    try {
      accept(await request('/rewrite', 'POST', { revision: report.draft.revision }));
      setReturnToVoice(true);
      if (telegram?.initData) {
        setRewriting('closing');
        // Paint the closing state before asking the host to dismiss the WebView.
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
        try { telegram.close(); }
        catch { setRewriting(null); }
        // Some hosts ignore close(); keep the Telegram return link available there.
        window.setTimeout(() => setRewriting(null), 2000);
      } else setRewriting(null);
    } catch (e) {
      setRewriting(null);
      setError(e instanceof Error ? e.message : 'Не удалось перезаписать черновик');
    }
    finally { setBusy(false); }
  }

  async function confirmReport(acknowledged = false) {
    if (!report || busy) return;
    if (!acknowledged && overruns.length) { setOverrunReview(true); return; }
    setOverrunReview(false);
    setBusy(true); setError('');
    try {
      accept(await request('/confirm', 'POST', { revision: report.draft.revision, lines: lines.map(editBody) }));
      setToast('Отчёт сохранён');
      if (telegram?.initData) telegram.HapticFeedback?.notificationOccurred('success');
    } catch (e) {
      try { const current = await request(); if (current.draft.status !== 'draft' || current.draft.revision !== report.draft.revision) accept(current); } catch { /* Retry reconciles a lost save response. */ }
      setError(e instanceof Error ? e.message : 'Не удалось сохранить отчёт');
    } finally { setBusy(false); }
  }

  async function editToday() {
    if (!report || !saved || busy) return;
    setBusy(true); setError('');
    try {
      const data = await request('/edit-today', 'POST', { revision: report.draft.revision });
      if (!data.edit_url) throw new Error('Не удалось открыть исправление. Повторите ещё раз.');
      window.location.assign(data.edit_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось открыть исправление');
      setBusy(false);
    }
  }

  async function addCard(position: Position) {
    if (!report || busy) return;
    const line: Line = { phrase: position.name, position_id: position.id, quantity: '0', unit: position.unit };
    setBusy(true);
    try {
      const next = [...lines, line];
      const data = await request('', 'PUT', { revision: report.draft.revision, lines: next.map(editBody) });
      accept(data); setPicker(false); setEmptyPicker(false); setToast('Карточка добавлена');
    } catch (e) { setError(e instanceof Error ? e.message : 'Не удалось добавить карточку'); }
    finally { setBusy(false); }
  }

  if (rewriting) return <main className="return-page rewrite-preloader" role="status" aria-live="polite" aria-busy="true">
    <LoaderCircle className="spin" size={36} aria-hidden="true" />
    <h1>{rewriting === 'closing' ? 'Возвращаемся в Telegram…' : 'Готовим перезапись…'}</h1>
    <p>{rewriting === 'closing' ? 'Закрываем окно отчёта.' : 'Бот готовится принять вашу голосовую команду.'}</p>
  </main>;

  if (returnToVoice) return <main className="return-page">
    <img className="brand-logo" src="/assets/m100-logo.png" alt="М100 · Инженерные сети" />
    <div className="return-icon"><Mic size={32} /></div>
    <h1>Продолжим в чате</h1>
    <p>Бот уже ждёт вашу команду. Запишите отчёт заново или расскажите, что отредактировать.</p>
    {report?.bot_url && <a className="button button-dark" href={report.bot_url}>Вернуться в Telegram <ArrowUpRight size={18} /></a>}
    <button className="button button-soft" onClick={() => setReturnToVoice(false)}>Вернуться к отчёту</button>
  </main>;

  return <>
    <header className="report-header">
      <a href="https://m-100.by/" target="_blank" rel="noreferrer" aria-label="Сайт М100"><img className="brand-logo" src="/assets/m100-logo.png" alt="М100 · Инженерные сети" /></a>
      {report && <span className="mobile-report-date">{new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long' }).format(new Date(report.draft.day + 'T12:00:00'))}</span>}
      <div className="header-copy">
      <span className="header-eyebrow">ГОЛОСОВОЙ ПОМОЩНИК</span>
      <h1>{saved ? 'Отчёт сохранён' : retrying ? 'Сохранение не завершено' : cancelled ? 'Черновик отменён' : 'Ваш отчёт о работах'}</h1>
      <p>{report ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(report.draft.day + 'T12:00:00')) : 'Продолжение вашей голосовой команды'}</p>
      </div>
      {profile && <a className="desktop-header-meta telegram-profile" href={profile?.url || undefined} target="_blank" rel="noreferrer" aria-label={profile ? `Telegram-аккаунт: ${profile.name}` : 'Telegram-аккаунт'}>
        <span className="telegram-avatar">{profilePhoto ? <img src={profilePhoto} alt="" onError={() => setProfilePhoto('')} /> : profileInitials}</span>
        <span className="telegram-profile-copy"><strong>{profile?.name || 'Telegram-аккаунт'}</strong><small>{profile?.username ? `@${profile.username}` : 'Личный аккаунт Telegram'}</small></span><Send size={15} className="telegram-profile-mark" />
      </a>}
    </header>
    <div className={`workspace-layout ${report ? 'with-dashboard' : ''}`}><section className="work-column" aria-label="Текущий отчёт">
    {report && <div className="desktop-work-heading"><div><span>ТЕКУЩИЙ ОТЧЁТ</span><h2>{saved ? 'Работы сохранены' : retrying ? 'Завершаем сохранение' : 'Ваши работы'}</h2><p>{saved ? 'Результат уже в таблице. Можно продолжить в чате.' : retrying ? report?.operation?.sheets_done ? 'Объёмы уже в Google Sheets. Повторите отправку в Битрикс24.' : 'Запись не завершена. Повторите сохранение.' : 'Проверьте объёмы — и можно сохранять.'}</p></div><span className="desktop-work-count">{String(visible.length).padStart(2, '0')}</span></div>}
    <main className={`cards-page ${report ? 'has-report-actions' : ''} ${saved ? 'has-saved-actions' : ''} ${report && error ? 'has-save-status' : ''}`} aria-label="Карточки работ">
      {loading ? <article className="state-card" role="status"><LoaderCircle className="spin" size={22} />Загружаем…</article>
        : !report ? <article className="state-card empty-report" aria-labelledby="empty-report-title">
            <img className="empty-report-art" src="/assets/empty-report-voice.png" width="192" height="192" alt="" />
            <h2 id="empty-report-title">Здесь появятся ваши работы</h2>
            <p className="empty-report-copy">Расскажите боту голосом, что сделали.<br />Он подготовит отчёт и пришлёт ссылку на ваши работы.</p>
            {error && <p className="empty-report-error" role="alert">{error}</p>}
            <a className="button button-dark empty-report-action" href="https://t.me/m100_work_reports_bot"><Send size={17} aria-hidden="true" />Перейти в бота</a>
            <p className="empty-report-help">Уже создали отчёт? Откройте его по ссылке из бота.</p>
          </article>
        : <>
          <div className="mobile-work-heading"><h1>{saved ? 'Работы сохранены' : cancelled ? 'Черновик отменён' : 'Ваши работы'}</h1><span>{visible.length} работ</span></div>
          <div className="mobile-work-tools">
            <div className="work-search"><Search size={21} aria-hidden="true" /><input type="search" aria-label="Поиск работ по названию или ID" placeholder="Поиск по работам…" value={workSearch} onChange={event => setWorkSearch(event.target.value)} />{workSearch && <button aria-label="Очистить поиск работ" onClick={() => setWorkSearch('')}><X size={18} /></button>}</div>
            <div className="work-filters" role="group" aria-label="Быстрый фильтр работ">{workFilters.map(filter => <button key={filter} aria-pressed={workFilter === filter} onClick={() => setWorkFilter(filter)}>{filter}</button>)}</div>
            <span className="visually-hidden" role="status">Показано работ: {filteredWorks.length} из {visible.length}</span>
          </div>
          {report.draft.mode === 'replace' && !saved && <p className="daily-edit-note"><PencilLine size={16} />Исправление за сегодня: объёмы заменят дневные значения.</p>}
          <div className="report-intro"><span>{saved ? 'Все изменения сохранены' : retrying ? 'Осталось завершить сохранение' : 'Проверьте перед сохранением'}</span><small>{visible.length} работ</small></div>
          {!!overruns.length && <aside className="estimate-warning" role="note" aria-label="Превышение сметы"><AlertTriangle size={20} /><div><strong>Объём превышает смету</strong>{overruns.map(item => <p key={item.id}>{item.name}: превышение {format(item.excess)} {item.unit}.</p>)}<small>Проверьте значения перед сохранением. Верный фактический объём можно сохранить.</small></div></aside>}
          {filteredWorks.map(({ line, index }) => {
            const position = effectivePosition(line);
            const figures = position ? cardFigures(position, report, lines) : null;
            return <button key={index} className="compact-card"
              onPointerDown={event => startCardPress(event, index)} onPointerMove={moveCardPress}
              onPointerUp={cancelCardPress} onPointerCancel={cancelCardPress} onPointerLeave={cancelCardPress}
              onContextMenu={event => { if (!locked) event.preventDefault(); }}
              onKeyDown={event => { if (event.key === 'Delete' && !locked) { event.preventDefault(); requestDelete(index); } }}
              onClick={event => { const held = pressConsumed.current; pressConsumed.current = false; if (held && event.detail > 0) { event.preventDefault(); return; } openCard(index); }}
              aria-keyshortcuts={locked ? undefined : 'Delete'}
              aria-label={`Открыть карточку ${index + 1}: ${position?.name || line.phrase}`}>
              <span className="mobile-work-card">
                <span className="mobile-card-icon"><ClipboardList size={21} strokeWidth={1.6} /></span>
                <span className="mobile-card-copy"><strong>{position?.name || line.phrase}</strong><small>Работа {String(index + 1).padStart(2, '0')}</small></span>
                <span className="mobile-card-quantity"><strong>{format(line.quantity)} <small>{position?.unit || line.unit}</small></strong><small>Объём</small></span>
                <span className="mobile-card-meta"><span className="mobile-card-id">{line.position_id || 'Без позиции'}</span><span className={`tile-badge ${workStatus(line) === 'Уточнить' ? 'warning' : ''}`}>{workStatus(line)}</span><span className="mobile-card-pay"><small>На руки</small>{format(figures?.completedPay)}</span></span>
              </span>
              <span className="desktop-card-content">
              <span className="tile-top"><span className="tile-label">Работа {index + 1} · {line.position_id || 'Выберите позицию'}</span><span className={`tile-badge ${!position || line.quantity == null || line.question ? 'warning' : ''}`}>{saved ? 'Сохранено' : !position || line.quantity == null || line.question ? 'Уточнить' : 'Распознано'}</span></span>
              <span className="tile-name">{position?.name || line.phrase}</span>
              <span className="tile-bottom"><span><small>Выполненный объём</small><strong>{format(line.quantity)} <em>{position?.unit || line.unit}</em></strong></span><span><small>На руки</small><strong>{format(figures?.completedPay)}</strong></span><span className="tile-arrow"><ArrowUpRight size={17} /></span></span>
              </span>
              {(line.quantity == null || line.question) && <span className="line-question">{line.question || 'Уточните выполненный объём'}</span>}
            </button>;
          })}
          {!!visible.length && !filteredWorks.length && <article className="state-card work-search-empty"><img className="work-search-empty-art" src="/assets/empty-work-search.svg" width="176" height="132" alt="Работы не найдены. Попробуйте другой запрос или фильтр." /><button className="button button-soft" onClick={resetWorkFilters}>Сбросить фильтры</button></article>}
          {!visible.length && <article className="state-card"><p>В отчёте пока нет работ.</p>{!locked && <button className="button button-soft" onClick={() => openPicker(true)}><Plus size={16} />Добавить работу</button>}</article>}
          {Boolean(visible.length) && !saved && !cancelled && <button className="button button-white add-card" disabled={locked || lines.length >= 40} onClick={() => openPicker(true)}><Plus size={17} />Добавить работу</button>}
          {report.draft.transcript && <>
            <button className="voice-transcript voice-transcript-trigger" aria-haspopup="dialog" aria-controls="voice-transcript-drawer" aria-expanded={transcriptOpen} onClick={() => setTranscriptOpen(true)}><span className="voice-block-icon"><Mic size={22} /></span><span className="voice-block-copy"><strong>Ваша голосовая команда</strong><small>Открыть расшифровку</small></span><ChevronDown className="voice-block-arrow" size={19} aria-hidden="true" /></button>
            <details className="voice-transcript desktop-voice-transcript"><summary><span className="voice-block-icon"><Mic size={22} /></span><span className="voice-block-copy"><strong>Ваша голосовая команда</strong><small>Посмотреть расшифровку</small></span><ChevronDown className="transcript-chevron" size={19} aria-hidden="true" /></summary><p>{report.draft.transcript}</p></details>
          </>}
          <SheetFeature tableUrl={report.table_url} className="mobile-sheet-feature" />
          {report.notification_pending && <p className="mobile-notification-note" role="status">Подтверждение в Telegram пока не доставлено. <button className="retry-notification" disabled={busy} onClick={() => void confirmReport()}>Отправить ещё раз</button></p>}
          <p className="report-hint">{saved ? 'Отчёт записан в исходную таблицу.' : 'Нажмите на работу, чтобы проверить детали. Удерживайте карточку, чтобы удалить. Для правок голосом нажмите «Перезаписать».'}{report.notification_pending && <> Подтверждение в Telegram пока не доставлено. <button className="retry-notification" disabled={busy} onClick={() => void confirmReport()}>Отправить ещё раз</button></>}{report.table_url && <> <a href={report.table_url} target="_blank" rel="noreferrer">Исходная таблица ↗</a></>}</p>
        </>}
    </main>

    {report && <footer className="page-actions report-actions" aria-label="Сохранение отчёта"><div className="report-actions-inner">
      {error && selected == null && <p className="page-error" role="alert">{error}</p>}
      {!saved && !cancelled && <><button className="button button-dark report-save" disabled={busy || (!retrying && (!visible.length && report.draft.mode !== 'replace'))} onClick={() => void confirmReport()}>{busy ? <LoaderCircle className="spin" size={18} /> : <Save size={18} />}{retrying ? 'Повторить сохранение' : 'Сохранить отчёт'}</button><button className="button button-white report-overwrite" title="Перезаписать голосом или рассказать, что исправить" aria-label="Перезаписать голосом" disabled={locked} onClick={overwriteDraft}><RotateCcw size={20} /><span className="mobile-overwrite-label">Перезаписать</span></button></>}
      {saved && <><span className="save-success"><CircleCheck size={20} />Сохранено в таблицу</span><button className="button button-white edit-today-action" disabled={busy} onClick={editToday}>{busy ? <LoaderCircle className="spin" size={18} /> : <PencilLine size={18} />}Исправить за сегодня</button></>}
    </div></footer>}
    </section>{report && <DesktopDashboard report={report} />}</div>

    <Drawer open={overrunReview} onClose={() => setOverrunReview(false)} className="position-dialog" label="Подтверждение превышения сметы">
      <div className="picker-heading"><h2>Проверить превышение</h2><button className="close-button" aria-label="Закрыть предупреждение" onClick={() => setOverrunReview(false)}><X size={20} /></button></div>
      <div className="overrun-review-body"><p>Фактический объём выше сметы. Убедитесь, что количество указано верно.</p>{overruns.map(item => <p key={item.id}><strong>{item.name}</strong><br />Превышение: {format(item.excess)} {item.unit}</p>)}</div>
      <div className="drawer-footer"><button className="button button-soft" onClick={() => setOverrunReview(false)}>Проверить объём</button><button className="button button-dark" onClick={() => void confirmReport(true)}>Сохранить с превышением</button></div>
    </Drawer>

    <Drawer open={transcriptOpen} onClose={() => setTranscriptOpen(false)} id="voice-transcript-drawer" className="transcript-drawer" labelledBy="transcript-title" compact>
      <div className="transcript-drawer-inner">
        <div className="transcript-drawer-heading"><span className="voice-block-icon"><Mic size={22} /></span><div><h2 id="transcript-title">Ваша голосовая команда</h2><p>Расшифровка отчёта</p></div><button className="close-button" aria-label="Закрыть расшифровку" onClick={() => setTranscriptOpen(false)}><X size={21} /></button></div>
        <div className="transcript-drawer-content"><p>{report?.draft.transcript}</p></div>
        <div className="transcript-drawer-footer"><button className="button button-dark" data-drawer-close>Готово</button></div>
      </div>
    </Drawer>

    <Drawer open={selected !== null} onClose={closeCard} canClose={!busy} className="detail-drawer quick-detail-drawer" label="Изменить объём" fullScreen>
      {form && <div className="drawer-inner"><div className="drawer-heading"><span className="card-editor-icon"><PencilLine size={21} /></span><div className="card-editor-heading"><span>{`РАБОТА ${String((selected ?? 0) + 1).padStart(2, '0')}${formPosition ? ` · ${formPosition}` : ''}`}</span><h2>Изменить объём</h2></div><button className="close-button" disabled={busy} aria-label="Закрыть карточку" onClick={closeCard}><X size={21} /></button></div>
        <div className="drawer-body quick-work-editor">
          <div className="quick-work-position"><small>{formPosition || 'Позиция не определена'}</small><h3>{form.name}</h3>{!locked && <button className="choose-position" disabled={busy} onClick={() => openPicker()}><Search size={15} />{formPosition ? 'Заменить работу' : 'Выбрать работу'}</button>}</div>
          <label className="edit-field volume-edit quick-volume"><span>Выполненный объём</span><div><input ref={volumeInputRef} aria-label="Выполненный объём" value={form.quantity} inputMode="decimal" enterKeyHint="done" autoComplete="off" disabled={locked} placeholder="0" onFocus={event => event.currentTarget.select()} onChange={event => editField('quantity', event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && dirty && !locked) { event.preventDefault(); void saveCard(); } }} /><span>{form.unit}</span></div></label>
          <p className="draft-edit-note">«Применить» изменит черновик. Чтобы записать результат в Google Sheets и Битрикс24, нажмите «Сохранить отчёт».</p>
          {selectedLine && !formPosition && <p className="drawer-error">Выберите работу из спецификации, чтобы применить объём.</p>}
          {error && <p className="drawer-error" role="alert">{error}</p>}
          <details className="work-reference" open={window.matchMedia('(min-width: 1000px)').matches}><summary>Информация о работе<ChevronDown size={16} aria-hidden="true" /></summary><div className="reference-grid">{detailFields.map(({ key, label }) => <div key={key}><span>{label}</span><strong>{key === 'unit' ? form.unit || '—' : format(form[key])}</strong></div>)}</div></details>
        </div>
        <div className="drawer-footer">{!saved && !cancelled && <><button className="button button-delete" disabled={locked} onClick={() => { if (selected !== null) requestDelete(selected); }}><Trash2 size={17} />Удалить</button><button className="button button-dark" disabled={locked || !dirty} onClick={saveCard}>{busy ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}Применить</button></>}{(saved || cancelled) && <span className="saved-note">{saved ? 'Отчёт сохранён' : 'Черновик отменён'}</span>}</div>
      </div>}
    </Drawer>

    <Drawer open={picker} onClose={() => setPicker(false)} canClose={!busy} className="position-dialog" label="Выбор позиции">
      <div className="picker-heading"><span className="voice-block-icon"><Search size={22} /></span><div className="picker-heading-copy"><h2>Выбор работы</h2><small>{allPositions ? 'Весь список позиций' : 'Подходящие позиции'}</small></div><button className="close-button" aria-label="Закрыть поиск" onClick={() => setPicker(false)}><X size={20} /></button></div>
      <div className="picker-scope">{allPositions ? <span>Все позиции из исходной таблицы</span> : <span>Подходящие варианты для этой работы</span>}{(!allPositions || !emptyPicker) && <button className="scope-button" onClick={() => setAllPositions(!allPositions)}>{allPositions ? 'Вероятные позиции' : 'Весь список'}</button>}</div>
      <label className="search-field"><Search size={18} /><input aria-label="Поиск по названию или ID" placeholder="Название или ID…" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <div className="catalog-list">{!pickerPositions.length && <p className="picker-empty">{allPositions ? 'По этому запросу ничего не найдено.' : 'Подходящих позиций не найдено. Откройте «Весь список» для поиска по всей таблице.'}</p>}{pickerPositions.map(position => <button className="catalog-item" key={position.id} disabled={busy} onClick={() => {
        if (selected != null && form && !emptyPicker) {
          const previewLines = lines.map((line, index) => index === selected ? { ...line, position_id: position.id, quantity: form.quantity } : line);
          const figures = cardFigures(position, report!, previewLines);
          setForm({ name: position.name, unit: position.unit, planned: position.planned, unit_pay: position.unit_pay || '', planned_pay: String(figures.plannedPay ?? ''), remaining: String(figures.remaining), completed_pay: String(figures.completedPay ?? ''), quantity: form.quantity });
          setFormPosition(position.id);
          setDirty(true); setPicker(false);
        } else void addCard(position);
      }}><span><small>{position.id}</small><strong>{position.name}</strong></span><span>{position.unit}</span></button>)}</div>
    </Drawer>

    <dialog ref={deleteDialogRef} className="delete-dialog" aria-labelledby="delete-title" aria-describedby="delete-description"
      onCancel={event => { event.preventDefault(); closeDelete(); }}
      onClick={event => { if (event.target === event.currentTarget) closeDelete(); }}
      onClose={() => { setDeleteIndex(null); setDeleteError(''); }}>
      <span className="delete-dialog-icon"><Trash2 size={24} /></span>
      <h2 id="delete-title">Удалить работу?</h2>
      <p id="delete-description">Точно хотите удалить эту работу из отчёта?</p>
      {deleteIndex !== null && lines[deleteIndex] && <p className="delete-work-name">{effectivePosition(lines[deleteIndex])?.name || lines[deleteIndex].phrase}</p>}
      <p className="delete-dialog-note">После удаления работу можно вернуть.</p>
      {deleteError && <p className="delete-dialog-error" role="alert">{deleteError}</p>}
      <div className="delete-dialog-actions"><button className="button button-soft" autoFocus disabled={busy} onClick={closeDelete}>Отмена</button><button className="button button-danger" disabled={locked} onClick={deleteCard}>{busy ? <LoaderCircle className="spin" size={17} /> : <Trash2 size={17} />}Удалить</button></div>
    </dialog>

    {toast && <div className="toast" role="status" aria-atomic="true"><CircleCheck size={17} aria-hidden="true" /><span>{toast}</span>{deleted && !locked && <button className="toast-undo" onClick={undoDeleted}><RotateCcw size={14} />Вернуть</button>}<button className="toast-close" aria-label="Закрыть уведомление" onClick={() => setToast('')}><X size={15} /></button></div>}
  </>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
