import { useState } from 'react';
import { api } from '../lib/transport';
import { Plus, Repeat, ShieldQuestion } from 'lucide-react';
import Drawer from './Drawer';
import { TaskIcon } from './ScheduledPanel';
import { useHud, useNow } from '../lib/store';
import { ago, humanizeName, whenLabel } from '../lib/format';
import type { ScheduledTask } from '../lib/types';
import PathText from './PathText';

// Every active scheduled task, with authenticated versioned controls. Grouped by what the user needs to
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
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [when, setWhen] = useState('');
  const act = async (action: string) => {
    if ((action === 'approve' || action === 'cancel') && !window.confirm(`${action === 'approve' ? 'Approve this pending action' : 'Cancel this task'}?\n${task.name}\n${task.preview || task.summary}\n${task.schedule}`)) return;
    setBusy(true); setError('');
    try { await api('/api/schedule/action', { method: 'POST', body: { action, id: task.id, version: task.version, confirmed: action === 'approve' || action === 'cancel', ...(action === 'reschedule' ? { when } : {}) } }); window.dispatchEvent(new Event('era:schedule')); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); window.dispatchEvent(new Event('era:schedule')); }
    finally { setBusy(false); }
  };
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
      <p className="mt-0.5 select-text text-meta leading-snug t3"><PathText text={task.summary} /></p>
      {task.awaiting && (
        <p className="mt-1.5 flex items-start gap-1.5 text-meta text-[var(--warn)]">
          <ShieldQuestion className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
          <span>{task.awaiting.reason || 'Waiting for your approval.'} Review the action, then approve or skip.</span>
        </p>
      )}
      {task.preview && <details className="text-meta t2 mt-2"><summary>Full action preview</summary><pre className="text-micro whitespace-pre-wrap break-words max-h-48 overflow-auto">{task.preview}</pre></details>}
      {last && (
        <p className="mt-1 font-mono text-micro t3">
          LAST RUN <span className={LAST_TONE[last.status] || 't2'}>{last.status.replace(/_/g, ' ').toUpperCase()}</span> · {ago(last.at, now)}
        </p>
      )}
      <div className="flex flex-wrap gap-2 mt-2">
        {task.awaiting ? <><button className="mini-btn" disabled={busy || task.previewComplete === false} onClick={() => act('approve')}>Approve</button><button className="mini-btn" disabled={busy} onClick={() => act('skip')}>Skip</button></> : <><button className="mini-btn" disabled={busy} onClick={() => act(task.enabled ? 'pause' : 'resume')}>{task.enabled ? 'Pause' : 'Resume'}</button><button className="mini-btn" disabled={busy || !task.enabled} onClick={() => act('run_now')}>Run now</button></>}
        <button className="mini-btn" disabled={busy} onClick={() => act('cancel')}>Cancel</button>
      </div>
      <form className="flex gap-2 mt-2" onSubmit={e => { e.preventDefault(); act('reschedule'); }}><input aria-label={`Reschedule ${task.name}`} className="min-w-0 flex-1 bg-transparent border border-[var(--line)] p-1 text-meta" placeholder="e.g. tomorrow at 09:00" maxLength={200} value={when} onChange={e => setWhen(e.target.value)} required /><button className="mini-btn" disabled={busy}>Reschedule</button></form>
      {error && <p role="alert" className="text-meta text-[var(--bad)] mt-1">{error}</p>}
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
  const [name, setName] = useState(''), [message, setMessage] = useState(''), [when, setWhen] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const create = async () => {
    setBusy(true); setError('');
    try { await api('/api/schedule/action', { method: 'POST', body: { action: 'create_reminder', name, message, when } }); setName(''); setMessage(''); setWhen(''); window.dispatchEvent(new Event('era:schedule')); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
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
        <form className="space-y-2" onSubmit={e => { e.preventDefault(); create(); }}>
          <h3 className="micro">NEW REMINDER</h3>
          <input className="w-full bg-transparent border border-[var(--line)] p-2 text-small" aria-label="Reminder name" placeholder="Name" maxLength={500} required value={name} onChange={e => setName(e.target.value)} />
          <input className="w-full bg-transparent border border-[var(--line)] p-2 text-small" aria-label="Reminder message" placeholder="Message" maxLength={500} required value={message} onChange={e => setMessage(e.target.value)} />
          <input className="w-full bg-transparent border border-[var(--line)] p-2 text-small" aria-label="Reminder schedule" placeholder="e.g. in 10 minutes" maxLength={500} required value={when} onChange={e => setWhen(e.target.value)} />
          <button className="mini-btn" disabled={busy}>Create reminder</button>{error && <p role="alert" className="text-meta text-[var(--bad)]">{error}</p>}
        </form>
        {!snap && <p className="text-small t3">Loading…</p>}
        {snap && !snap.ok && <p className="text-small text-[var(--warn)]">The schedule could not be read{snap.error ? ` (${snap.error})` : ''}.</p>}
        {snap?.ok && tasks.length === 0 && <p className="text-small t3">No scheduled tasks. Ask ERA ONE to schedule something.</p>}
        <Group title="NEEDS APPROVAL" tasks={awaiting} now={now} />
        <Group title="UPCOMING" tasks={upcoming} now={now} />
        <Group title="PAUSED" tasks={paused} now={now} />
        {snap?.ok && tasks.length > 0 && (
          <p className="border-t border-[var(--line)] pt-3 text-meta leading-relaxed t3">
            Task actions retain the scheduler’s approval checks.
            {stale && ' * last stored time — the live scheduler is not reporting for that task.'}
          </p>
        )}
      </div>
    </Drawer>
  );
}
