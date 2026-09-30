import { Plus, Repeat, ShieldQuestion } from 'lucide-react';
import Drawer from './Drawer';
import { TaskIcon } from './ScheduledPanel';
import { useHud, useNow } from '../lib/store';
import { ago, humanizeName, whenLabel } from '../lib/format';
import type { ScheduledTask } from '../lib/types';

// Every active scheduled task, read-only. Grouped by what the user needs to
// know: waiting for their approval, coming up, paused. Finished tasks are not
// listed (the backend leaves them out).

const LAST_TONE: Record<string, string> = {
  ok: 'text-[var(--ok)]',
  partial: 'text-[var(--warn)]',
  failed: 'text-[var(--bad)]',
  awaiting_confirmation: 'text-[var(--warn)]',
  skipped: 't3',
  missed: 'text-[var(--warn)]',
};

function Task({ task, now }: { task: ScheduledTask; now: number }) {
  const last = task.lastRun;
  return (
    <article className="sched-card">
      <div className="flex items-center gap-2">
        <TaskIcon task={task} />
        <h4 className="min-w-0 flex-1 truncate text-body font-medium t1" title={task.name}>{humanizeName(task.name)}</h4>
        {task.enabled && task.nextRun !== null && (
          <span className="shrink-0 font-mono text-meta tabular-nums t1" title={new Date(task.nextRun * 1000).toLocaleString()}>
            {whenLabel(task.nextRun, now)}{task.nextRunLive ? '' : ' *'}
          </span>
        )}
      </div>
      <p className="mt-1 flex items-center gap-1.5 text-meta t2">
        {task.recurring && <Repeat className="h-3 w-3 shrink-0 t3" aria-hidden="true" />}
        {task.schedule}
      </p>
      <p className="mt-0.5 select-text text-meta leading-snug t3">{task.summary}</p>
      {task.awaiting && (
        <p className="mt-1.5 flex items-start gap-1.5 text-meta text-[var(--warn)]">
          <ShieldQuestion className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
          <span>{task.awaiting.reason || 'Waiting for your approval.'} Say “yes”, “always” or “skip”.</span>
        </p>
      )}
      {last && (
        <p className="mt-1 font-mono text-micro t3">
          LAST RUN <span className={LAST_TONE[last.status] || 't2'}>{last.status.replace(/_/g, ' ').toUpperCase()}</span> · {ago(last.at, now)}
        </p>
      )}
    </article>
  );
}

function Group({ title, tasks, now }: { title: string; tasks: ScheduledTask[]; now: number }) {
  if (!tasks.length) return null;
  return (
    <section className="space-y-2">
      <h3 className="micro">{title} · {tasks.length}</h3>
      {tasks.map(t => <Task key={t.id} task={t} now={now} />)}
    </section>
  );
}

export default function ScheduleDrawer({ onSchedule }: { onSchedule: () => void }) {
  const snap = useHud(s => s.schedule);
  const now = useNow();
  const tasks = snap?.tasks || [];
  const awaiting = tasks.filter(t => t.awaiting);
  const upcoming = tasks.filter(t => !t.awaiting && t.enabled);
  const paused = tasks.filter(t => !t.awaiting && !t.enabled);
  const stale = tasks.some(t => t.enabled && t.nextRun !== null && !t.nextRunLive);

  return (
    <Drawer
      title="SCHEDULED TASKS"
      actions={
        <button type="button" className="mini-btn" onClick={onSchedule} title="Pre-fill the command bar — nothing is sent until you press Enter">
          <Plus className="h-3 w-3" /> Schedule
        </button>
      }
    >
      <div className="space-y-5 p-4">
        {!snap && <p className="text-small t3">Loading…</p>}
        {snap && !snap.ok && <p className="text-small text-[var(--warn)]">The schedule could not be read{snap.error ? ` (${snap.error})` : ''}.</p>}
        {snap?.ok && tasks.length === 0 && <p className="text-small t3">No scheduled tasks. Ask ERA ONE to schedule something.</p>}
        <Group title="NEEDS APPROVAL" tasks={awaiting} now={now} />
        <Group title="UPCOMING" tasks={upcoming} now={now} />
        <Group title="PAUSED" tasks={paused} now={now} />
        {snap?.ok && tasks.length > 0 && (
          <p className="border-t border-[var(--line)] pt-3 text-meta leading-relaxed t3">
            To create, change, pause or cancel a task, ask ERA ONE.
            {stale && ' * last stored time — the live scheduler is not reporting for that task.'}
          </p>
        )}
      </div>
    </Drawer>
  );
}
