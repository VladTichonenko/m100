import { ArrowUpRight, Check, ChevronRight, FileSpreadsheet } from 'lucide-react';

export function SheetFeature({ tableUrl, className = '' }: { tableUrl: string; className?: string }) {
  return <a className={`sheet-feature dashboard-tile ${className}`} href={tableUrl || undefined} target="_blank" rel="noreferrer" aria-label="Открыть исходную таблицу" aria-disabled={!tableUrl} onClick={event => { if (!tableUrl) event.preventDefault(); }}>
      <span className="feature-eyebrow"><FileSpreadsheet size={17} />GOOGLE SHEETS</span>
      <h2>Всё начинается<br />с таблицы.</h2>
      <p>Спецификация, объёмы<br />и результаты вашей работы.</p>
      <span className="feature-link">Открыть таблицу <ArrowUpRight size={16} /></span>
      <div className="sheet-art" aria-hidden="true">
        <div className="sheet-back" />
        <div className="mini-sheet"><div className="mini-sheet-heading"><span className="sheet-app-icon"><FileSpreadsheet size={19} /></span><span>М100 · Спецификация<small>Учёт выполненных работ</small></span><span className="mini-sheet-dots">•••</span></div>
          <div className="mini-sheet-toolbar"><span>Файл</span><span>Правка</span><span>Вид</span><span>Вставка</span></div>
          <div className="mini-sheet-grid"><div className="mini-sheet-row mini-sheet-letters"><span /><span>A</span><span>B</span><span>C</span></div>
            {['Наименование', 'Фреонопровод', 'Изоляция труб', 'Кабель силовой', 'Крепёж'].map((name, index) => <div className={`mini-sheet-row ${index === 2 ? 'highlighted' : ''}`} key={name}><span>{index + 1}</span><span>{name}</span><span>{index === 0 ? 'Объём' : '—'}</span><span>{index === 0 ? 'Ед.' : 'м'}</span></div>)}
          </div>
          <div className="mini-sheet-bottom"><span>＋</span><span>Лист 1 <ChevronRight size={10} /></span></div>
        </div>
        <span className="floating-sheet-label"><Check size={12} />Всё в одном месте</span>
      </div>
      <span className="feature-orbit feature-orbit-one" /><span className="feature-orbit feature-orbit-two" />
    </a>;
}
