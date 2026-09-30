import { getState, setState } from './store';
import type { AgentEvent, Notice, NoticeAction, NoticeKind } from './types';

// ERA ONE notifications — the top rail (components/NotificationRail.tsx).
// Every notice comes from a REAL backend event (noticeFromEvent) or a user
// action (the HUD's own file send). Rules, per the roadmap:
//   success / info / warn  auto-dismiss (timers pause while hovered/focused)
//   error                  persists until dismissed (or resolved at the source)
//   approval               auto-dismisses after 10 s (never parked on screen);
//                          the backend still holds it — "yes"/"no" still work
// Anti-spam: routine foreground work never notifies (it is already in Live
// Activity and the conversation); repeats with the same key fold into one
// notice with a count instead of stacking.

const MAX_NOTICES = 20;
export const TTL_MS: Record<NoticeKind, number | null> = {
  success: 6000,
  info: 4000,
  warn: 8000,
  error: null,
  approval: 10000,
};

let seq = 0;

export function notify(n: {
  kind: NoticeKind;
  title: string;
  message?: string;
  key?: string;
  action?: NoticeAction;
}): void {
  const now = Date.now();
  setState(s => {
    const list = s.notices;
    const i = n.key ? list.findIndex(x => x.key === n.key) : -1;
    if (i >= 0) {
      // Same thing again: refresh it in place and count, never stack.
      const prev = list[i];
      const next = { ...prev, ...n, message: n.message ?? prev.message, count: prev.count + 1, ts: now, rev: prev.rev + 1 };
      return { notices: [next, ...list.slice(0, i), ...list.slice(i + 1)] };
    }
    const fresh: Notice = { id: `n${++seq}`, count: 1, ts: now, rev: 0, message: '', ...n };
    return { notices: [fresh, ...list].slice(0, MAX_NOTICES) };
  });
}

export function dismiss(id: string): void {
  setState(s => ({ notices: s.notices.filter(n => n.id !== id) }));
}

/** Remove notices whose cause is gone (e.g. the approval was answered). */
export function resolve(key: string): boolean {
  const had = getState().notices.some(n => n.key === key);
  if (had) setState(s => ({ notices: s.notices.filter(n => n.key !== key) }));
  return had;
}

const LINK_DOWN = new Set(['cooldown', 'auth', 'config', 'offline']);

/** The one mapping from backend events to notices. Called by the transport. */
export function noticeFromEvent(e: AgentEvent): void {
  const md = (e.metadata || {}) as Record<string, unknown>;
  switch (e.type) {
    case 'error':
      notify({
        kind: 'error',
        key: `error:${e.message || ''}`,
        title: 'Something went wrong',
        message: e.message || 'An engine error was reported.',
        action: { label: 'Details', type: 'drawer', arg: 'diagnostics' },
      });
      break;
    case 'tool.failed':
      if (md.background) break;
      if (md.status === 'failed') {
        notify({
          kind: 'error',
          key: `tool:${e.toolName || ''}`,
          title: `${String(md.label || e.toolName || 'Tool')} failed`,
          message: e.message || String(md.activity || ''),
          action: { label: 'Activity', type: 'panel', arg: 'left' },
        });
      } else if (md.status === 'unknown') {
        notify({
          kind: 'warn',
          key: `tool:${e.toolName || ''}`,
          title: `${String(md.label || e.toolName || 'Tool')}: no result reported`,
          message: String(md.activity || ''),
          action: { label: 'Activity', type: 'panel', arg: 'left' },
        });
      }
      break;
    case 'tool.completed':
      // Only background work: the user is not watching it happen.
      if (md.background) {
        notify({
          kind: 'success',
          key: `done:${e.toolCallId || e.toolName}`,
          title: `${String(md.label || e.toolName || 'Task')} finished`,
          message: e.message || String(md.activity || ''),
          action: { label: 'Activity', type: 'panel', arg: 'left' },
        });
      }
      break;
    case 'approval.requested':
      notify({
        kind: 'approval',
        key: 'approval',
        title: 'Action required',
        message: `ERA ONE wants to ${String(md.title || e.label || md.tool || 'do something')}`,
        action: { label: 'Review', type: 'focus-approval' },
      });
      break;
    case 'approval.resolved':
      resolve('approval');
      break;
    case 'connection.status':
      if (LINK_DOWN.has(e.state || '')) {
        notify({
          kind: 'error',
          key: 'model-link',
          title: `Model link: ${e.label || 'down'}`,
          message: e.message || 'ERA ONE cannot reach its model right now.',
          action: { label: 'Details', type: 'drawer', arg: 'diagnostics' },
        });
      } else if ((e.state === 'connected' || e.state === 'local') && resolve('model-link')) {
        notify({ kind: 'success', key: 'model-link-ok', title: 'Model link restored', message: e.label || '' });
      }
      break;
  }
}
