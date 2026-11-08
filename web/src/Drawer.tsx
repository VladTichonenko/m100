import { useEffect, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from 'react';
import { resolveDrawerGesture } from './drawer-gestures';

type Props = {
  open: boolean;
  onClose: () => void;
  canClose?: boolean;
  className?: string;
  id?: string;
  label?: string;
  labelledBy?: string;
  compact?: boolean;
  children: ReactNode;
};

// Nested drawers retain the body lock until the last panel has finished closing.
let lockCount = 0;
let previousOverflow = '';
let previousPadding = '';
function lockScroll() {
  if (lockCount++ === 0) {
    previousOverflow = document.body.style.overflow;
    previousPadding = document.body.style.paddingRight;
    const gutter = window.innerWidth - document.documentElement.clientWidth;
    if (gutter > 0) document.body.style.paddingRight = `${parseFloat(getComputedStyle(document.body).paddingRight) + gutter}px`;
    document.body.style.overflow = 'hidden';
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (--lockCount === 0) {
      document.body.style.overflow = previousOverflow;
      document.body.style.paddingRight = previousPadding;
    }
  };
}

const mobile = () => window.matchMedia('(max-width: 999px)').matches;
const duration = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 280;

export function Drawer({ open, onClose, canClose = true, className = '', id, label, labelledBy, compact, children }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const animation = useRef<Animation | null>(null);
  const unlock = useRef<(() => void) | null>(null);
  const closing = useRef(false);
  const dismiss = useRef(onClose);
  const dismissible = useRef(canClose);
  const cachedChildren = useRef(children);
  const [phase, setPhase] = useState('closed');
  const [expanded, setExpanded] = useState(false);
  const [drag, setDrag] = useState<{ offset: number; height?: number } | null>(null);
  const gesture = useRef<{ y: number; started: number; height: number; expanded: boolean } | null>(null);
  const dragged = useRef(false);
  dismiss.current = onClose;
  dismissible.current = canClose;
  if (open) cachedChildren.current = children;

  function finishClose() {
    const panel = dialog.current;
    if (!panel?.open) return;
    panel.close();
    closing.current = false;
    animation.current?.cancel();
    animation.current = null;
    setPhase('closed'); setExpanded(false); setDrag(null);
    gesture.current = null; dragged.current = false;
    unlock.current?.(); unlock.current = null;
    dismiss.current();
  }

  function close(force = false) {
    const panel = dialog.current;
    if (!panel?.open || closing.current || (!force && !dismissible.current)) return;
    closing.current = true;
    const from = getComputedStyle(panel).transform;
    animation.current?.cancel();
    setPhase('closing');
    const motion = panel.animate([
      { transform: from === 'none' ? 'translateY(0)' : from, opacity: 1 },
      { transform: mobile() ? 'translateY(100%)' : 'translateY(16px) scale(.97)', opacity: mobile() ? 1 : 0 },
    ], { duration: duration(), easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
    animation.current = motion;
    motion.finished.then(finishClose).catch(() => {});
  }

  useEffect(() => {
    const panel = dialog.current;
    if (!panel) return;
    if (open && !panel.open) {
      closing.current = false; setExpanded(false); setDrag(null);
      panel.showModal();
      unlock.current = lockScroll();
      setPhase('opening');
      const motion = panel.animate([
        { transform: mobile() ? 'translateY(100%)' : 'translateY(16px) scale(.97)', opacity: mobile() ? 1 : 0 },
        { transform: 'translateY(0)', opacity: 1 },
      ], { duration: duration(), easing: 'cubic-bezier(.22,1,.36,1)' });
      animation.current = motion;
      motion.finished.then(() => { if (!closing.current) { setPhase('open'); animation.current = null; } }).catch(() => {});
    } else if (!open && panel.open) close(true);
  }, [open]);

  useEffect(() => () => { animation.current?.cancel(); dialog.current?.close(); unlock.current?.(); }, []);

  function beginDrag(event: PointerEvent<HTMLButtonElement>) {
    if (!mobile() || closing.current || event.button !== 0 || !event.isPrimary) return;
    animation.current?.finish();
    gesture.current = { y: event.clientY, started: performance.now(), height: dialog.current!.getBoundingClientRect().height, expanded };
    dragged.current = false;
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: PointerEvent<HTMLButtonElement>) {
    const start = gesture.current;
    if (!start) return;
    const distance = event.clientY - start.y;
    if (Math.abs(distance) > 6) dragged.current = true;
    if (distance < 0) setDrag({ offset: 0, height: Math.min(window.innerHeight, start.height - distance) });
    else setDrag({ offset: distance });
  }

  function endDrag(event: PointerEvent<HTMLButtonElement>) {
    const start = gesture.current;
    if (!start) return;
    gesture.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    const result = resolveDrawerGesture(event.clientY - start.y, performance.now() - start.started, start.expanded);
    if (result === 'close') { close(); setDrag(null); }
    else { if (result !== 'stay') setExpanded(result === 'expand'); setDrag(null); }
  }

  const style = {
    '--drawer-rest-height': compact ? 'min(64dvh, 560px)' : '84dvh',
    ...(drag ? { transform: `translateY(${drag.offset}px)`, ...(drag.height ? { height: `${drag.height}px` } : {}) } : {}),
  } as CSSProperties;

  return <dialog ref={dialog} id={id} className={`app-drawer ${className}`} aria-label={label} aria-labelledby={labelledBy}
    data-state={phase} data-expanded={expanded} data-dragging={Boolean(drag)} style={style}
    onCancel={event => { event.preventDefault(); close(); }}
    onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <button type="button" className="drawer-grip" aria-label={expanded ? 'Свернуть панель' : 'Развернуть на весь экран'} aria-expanded={expanded}
      aria-keyshortcuts="ArrowUp ArrowDown" title="Потяните вверх, чтобы развернуть, или вниз, чтобы закрыть"
      disabled={phase === 'closing'} onPointerDown={beginDrag} onPointerMove={moveDrag} onPointerUp={endDrag}
      onPointerCancel={() => { gesture.current = null; dragged.current = true; setDrag(null); }}
      onClick={() => { if (dragged.current) { dragged.current = false; return; } setExpanded(value => !value); }}
      onKeyDown={event => {
        if (event.key === 'ArrowUp') { event.preventDefault(); setExpanded(true); }
        if (event.key === 'ArrowDown') { event.preventDefault(); if (expanded) setExpanded(false); else close(); }
      }}><span /></button>
    <div className="app-drawer-content" inert={phase === 'closing'}
      onClickCapture={event => {
        // Existing close buttons and the transcript's Done action share the exit animation.
        const button = (event.target as HTMLElement).closest('[data-drawer-close], .close-button');
        if (button && !button.hasAttribute('disabled')) { event.stopPropagation(); close(); }
      }}>{open ? children : phase !== 'closed' ? cachedChildren.current : null}</div>
  </dialog>;
}
