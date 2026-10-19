import { memo, useEffect, useState, type CSSProperties } from 'react';
import { AlertTriangle, CheckCircle2, ChevronRight, Info, ShieldQuestion, X } from 'lucide-react';
import { DOCK_QUERY, setState, useHud, useNow } from '../lib/store';
import { dismiss, TTL_MS } from '../lib/notifications';
import { ago } from '../lib/format';
import type { Notice, NoticeAction, NoticeKind } from '../lib/types';
import PathText from './PathText';

// The top notification rail. State colour is never the only signal: every
// notice carries an icon AND a text label for its kind. Every notice leaves
// after 15 s (a hairline shows the time left); the timer pauses while the
// pointer or keyboard focus is on a notice; Esc dismisses the
// focused one. Hidden while the backend link is down (the offline banner owns
// that moment, and no new events can arrive).

const MAX_VISIBLE = 3;

const KIND: Record<NoticeKind, { label: string; Icon: typeof Info; cls: string }> = {
  success: { label: 'DONE', Icon: CheckCircle2, cls: 'notice-ok' },
  info: { label: 'INFO', Icon: Info, cls: 'notice-info' },
  warn: { label: 'WARNING', Icon: AlertTriangle, cls: 'notice-warn' },
  error: { label: 'ERROR', Icon: AlertTriangle, cls: 'notice-bad' },
  approval: { label: 'ACTION REQUIRED', Icon: ShieldQuestion, cls: 'notice-warn' },
};

function runAction(a: NoticeAction): void {
  if (a.type === 'drawer' && a.arg) {
    setState({ drawer: a.arg as never });
  } else if (a.type === 'panel') {
    if (document.querySelector('.era-canvas')) {
      const widget = document.querySelector<HTMLElement>(a.arg === 'right' ? '[data-widget=context] .era-peek-button' : '[data-widget=activity] .era-peek-button');
      widget?.click(); return;
    }
    const side = a.arg === 'right' ? 'right' : 'left';
    if (window.matchMedia(DOCK_QUERY[side]).matches) setState(side === 'left' ? { showLeft: true } : { showRight: true });
    else setState({ overlay: side });
  } else if (a.type === 'focus-approval') {
    // Focus the prompt itself — never its Approve button (a stray Enter must not approve).
    const el = document.querySelector<HTMLElement>('.approval');
    el?.scrollIntoView({ block: 'nearest' });
    el?.focus();
  }
}

function Toast({ n }: { n: Notice }) {
  const k = KIND[n.kind];
  const ttl = TTL_MS[n.kind];
  const [held, setHeld] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const close = () => setLeaving(true);
  useEffect(() => { setLeaving(false); }, [n.rev]);
  useEffect(() => {
    if (!leaving) return;
    const t = window.setTimeout(() => dismiss(n.id), 220);
    return () => window.clearTimeout(t);
  }, [leaving, n.id, n.rev]);
  const now = useNow();

  // The countdown IS the hairline's CSS animation: hover/focus pauses it
  // (animation-play-state) and it resumes where it stopped; a repeat (new rev)
  // remounts it from full. Its animationend closes the notice.

  return (
    <li
      className={`notice ${k.cls} ${leaving ? 'is-leaving' : ''} ${held ? 'is-held' : ''}`}
      role={n.kind === 'error' ? 'alert' : 'status'}
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setHeld(false); }}
      onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); close(); } }}
    >
      {ttl !== null && <i key={n.rev} className="notice-timer" style={{ '--ttl': `${ttl}ms` } as CSSProperties} aria-hidden="true" onAnimationEnd={close} />}
      <k.Icon className="notice-icon h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="notice-kind micro">{k.label}</span>
          {n.count > 1 && <span className="chip chip-dim">×{n.count}</span>}
          <span className="ml-auto shrink-0 font-mono text-micro t3">{ago(n.ts / 1000, now)}</span>
        </div>
        <p className="truncate text-meta font-medium t1" title={n.title}>{n.title}</p>
        {n.message && <p className="notice-msg select-text text-meta t2" title={n.message}><PathText text={n.message} short /></p>}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <button type="button" className="icon-btn icon-btn-sm notice-close" aria-label="Dismiss notification" title="Dismiss (Esc)" onClick={close}>
          <X className="h-3.5 w-3.5" />
        </button>
        {n.action && (
          <button type="button" className="notice-action" onClick={() => runAction(n.action!)}>
            {n.action.label} <ChevronRight className="h-3 w-3" />
          </button>
        )}
      </div>
    </li>
  );
}

function NotificationRail() {
  const notices = useHud(s => s.notices);
  const online = useHud(s => s.link === 'online');
  const [all, setAll] = useState(false);
  if (!online || notices.length === 0) return null;
  // Approval first (it is waiting on the user), then newest.
  const ordered = [...notices].sort((a, b) => Number(b.kind === 'approval') - Number(a.kind === 'approval'));
  const shown = all ? ordered : ordered.slice(0, MAX_VISIBLE);
  const hidden = ordered.length - shown.length;
  return (
    <section className="notice-rail" aria-label="Notifications">
      <ul className="space-y-1.5">
        {shown.map(n => <Toast key={n.id} n={n} />)}
      </ul>
      {(hidden > 0 || all) && (
        <div className="mt-1 flex justify-center gap-2">
          <button type="button" className="notice-more" aria-expanded={all} onClick={() => setAll(x => !x)}>
            {all ? 'Show fewer' : `+${hidden} more`}
          </button>
          <button type="button" className="notice-more" onClick={() => { setState({ notices: [] }); setAll(false); }}>
            Dismiss all
          </button>
        </div>
      )}
    </section>
  );
}

export default memo(NotificationRail);
