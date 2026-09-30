import { memo, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronRight, Info, ShieldQuestion, X } from 'lucide-react';
import { DOCK_QUERY, setState, useHud, useNow } from '../lib/store';
import { dismiss, TTL_MS } from '../lib/notifications';
import { ago } from '../lib/format';
import type { Notice, NoticeAction, NoticeKind } from '../lib/types';

// The top notification rail. State colour is never the only signal: every
// notice carries an icon AND a text label for its kind. Auto-dismiss timers
// pause while the pointer or keyboard focus is on a notice; Esc dismisses the
// focused one. Hidden while the backend link is down (the offline banner owns
// that moment, and no new events can arrive).

const MAX_VISIBLE = 4;

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
  const now = useNow();

  useEffect(() => {
    if (ttl === null || held) return;
    const t = window.setTimeout(() => dismiss(n.id), ttl);
    return () => window.clearTimeout(t);
  }, [ttl, held, n.id, n.rev]);

  return (
    <li
      className={`notice ${k.cls}`}
      role={n.kind === 'error' ? 'alert' : 'status'}
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setHeld(false); }}
      onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); dismiss(n.id); } }}
    >
      <k.Icon className="notice-icon h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="notice-kind micro">{k.label}</span>
          {n.count > 1 && <span className="chip chip-dim">×{n.count}</span>}
          <span className="ml-auto shrink-0 font-mono text-micro t3">{ago(n.ts / 1000, now)}</span>
        </div>
        <p className="mt-0.5 truncate text-small font-medium t1" title={n.title}>{n.title}</p>
        {n.message && <p className="notice-msg select-text text-meta t2" title={n.message}>{n.message}</p>}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <button type="button" className="icon-btn icon-btn-sm" aria-label="Dismiss notification" title="Dismiss (Esc)" onClick={() => dismiss(n.id)}>
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
      <ul className="space-y-2">
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
