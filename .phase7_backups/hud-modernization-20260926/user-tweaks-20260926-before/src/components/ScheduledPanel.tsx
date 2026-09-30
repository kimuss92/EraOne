import { memo } from 'react';
import { Bell, ChevronRight, ListOrdered, Plus, Repeat, ShieldQuestion, Zap } from 'lucide-react';
import { setState, useHud, useNow } from '../lib/store';
import { humanizeName, whenLabel } from '../lib/format';
import type { ScheduledTask } from '../lib/types';

// SCHEDULED — future work, deliberately separate from Live Activity (what is
// happening now). Read-only mirror of core/task_scheduler via /api/schedule
// (lib/schedule.ts keeps it fresh). Creating or changing a task stays with
// ERA ONE: "+ Schedule" only pre-fills the command bar; nothing is sent until
// the user presses Enter.

export const KIND_ICON: Record<string, typeof Bell> = { reminder: Bell, chain: ListOrdered, tool: Zap };
const SHOWN = 3;

export function TaskIcon({ task }: { task: ScheduledTask }) {
  const Icon = KIND_ICON[task.kind] || Zap;
  return <Icon className="h-3.5 w-3.5 shrink-0 t3" aria-hidden="true" />;
}

function Row({ task, now }: { task: ScheduledTask; now: number }) {
  const title = `${task.name} — ${task.schedule}\n${task.summary}`;
  if (task.awaiting) {
    return (
      <li className="sched-row sched-row-warn" title={title}>
        <ShieldQuestion className="h-3.5 w-3.5 shrink-0 text-[var(--warn)]" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate text-small t1">{humanizeName(task.name)}</span>
        <span className="shrink-0 font-mono text-micro text-[var(--warn)]">NEEDS APPROVAL</span>
      </li>
    );
  }
  return (
    <li className="sched-row" title={title}>
      <TaskIcon task={task} />
      <span className="min-w-0 flex-1 truncate text-small t2">{humanizeName(task.name)}</span>
      {task.recurring && <Repeat className="h-3 w-3 shrink-0 t3" aria-label="recurring" />}
      <span className="shrink-0 whitespace-nowrap font-mono text-meta tabular-nums t1">{whenLabel(task.nextRun, now)}</span>
    </li>
  );
}

function ScheduledPanel({ onSchedule }: { onSchedule: () => void }) {
  const snap = useHud(s => s.schedule);
  const now = useNow();
  const tasks = snap?.tasks || [];
  const awaiting = tasks.filter(t => t.awaiting);
  const upcoming = tasks.filter(t => !t.awaiting && t.enabled && t.nextRun !== null);
  const shown = [...awaiting.slice(0, 2), ...upcoming.slice(0, SHOWN)];
  const active = awaiting.length + upcoming.length;

  return (
    <section className="panel shrink-0" aria-label="Scheduled tasks">
      <div className="panel-head">
        <span className="micro panel-title">SCHEDULED</span>
        {snap?.ok && !snap.running && tasks.length > 0 && (
          <span className="chip chip-warn" title="The task scheduler is not running; times are the last ones stored">SCHEDULER OFF</span>
        )}
        {awaiting.length > 0 && <span className="chip chip-warn">{awaiting.length} TO APPROVE</span>}
        <span className="ml-auto font-mono text-micro t3">{snap?.ok ? active : ''}</span>
      </div>
      <div className="px-3 pb-2 pt-2">
        {!snap && <p className="px-1 text-meta t3">Loading scheduled tasks…</p>}
        {snap && !snap.ok && (
          <p className="px-1 text-meta text-[var(--warn)]">
            {snap.error === 'restart' ? 'Restart ERA ONE to show scheduled tasks here.' : 'Schedule unavailable.'}
          </p>
        )}
        {snap?.ok && shown.length === 0 && <p className="px-1 text-meta t3">No upcoming tasks.</p>}
        {shown.length > 0 && (
          <ul className="space-y-0.5">
            {shown.map(t => <Row key={t.id} task={t} now={now} />)}
          </ul>
        )}
        <div className="mt-1.5 flex items-center justify-between">
          <button type="button" className="sched-link" onClick={onSchedule} title="Pre-fill the command bar — nothing is sent until you press Enter">
            <Plus className="h-3 w-3" /> Schedule
          </button>
          {snap?.ok && tasks.length > 0 && (
            <button type="button" className="sched-link" onClick={() => setState({ drawer: 'schedule' })}>
              View all{tasks.length > shown.length ? ` ${tasks.length}` : ''} <ChevronRight className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

export default memo(ScheduledPanel);
