import { memo, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Ban,
  Brain,
  Check,
  ChevronDown,
  CircleDashed,
  Clock3,
  CloudSun,
  Cpu,
  FileText,
  Globe,
  Hand,
  Loader2,
  Mail,
  MessageCircle,
  Mic,
  Monitor,
  Music,
  ShieldQuestion,
  Sparkles,
  Workflow,
  X,
} from 'lucide-react';
import { useHud } from '../lib/store';
import { actions } from '../lib/transport';
import { clock, duration } from '../lib/format';
import type { ToolRun, ToolStatus } from '../lib/types';
import PathText from './PathText';
import { splitPaths } from './PathText';
import { FileActions, RecentFiles } from './FileActions';
import TaskProgress from './TaskProgress';
import { useDevelopment, working } from '../lib/development';

export const CATEGORY_ICON: Record<string, typeof Globe> = {
  web: Globe,
  weather: CloudSun,
  media: Music,
  memory: Brain,
  files: FileText,
  email: Mail,
  message: MessageCircle,
  system: Monitor,
  apps: Monitor,
  browser: Globe,
  voice: Mic,
  vision: Sparkles,
  task: Clock3,
  cm: Workflow,
  safety: ShieldQuestion,
  tool: Cpu,
};

const STATUS: Record<ToolStatus, { label: string; cls: string; Icon: typeof Check }> = {
  queued: { label: 'QUEUED', cls: 'chip-dim', Icon: CircleDashed },
  running: { label: 'RUNNING', cls: 'chip-live', Icon: Loader2 },
  success: { label: 'COMPLETE', cls: 'chip-ok', Icon: Check },
  failed: { label: 'FAILED', cls: 'chip-bad', Icon: AlertTriangle },
  cancelled: { label: 'CANCELLED', cls: 'chip-dim', Icon: Ban },
  blocked: { label: 'BLOCKED', cls: 'chip-warn', Icon: Hand },
  awaiting: { label: 'NEEDS APPROVAL', cls: 'chip-warn', Icon: ShieldQuestion },
  unknown: { label: 'NO RESULT', cls: 'chip-warn', Icon: CircleDashed },
};

// Priority: work that is happening or needs the user stays a full card;
// finished work recedes to a one-line row (still expandable for details).
const ACTIVE = new Set<ToolStatus>(['running', 'queued', 'awaiting']);
const SETTLED = new Set<ToolStatus>(['success', 'cancelled']);

const CARD_TONE: Partial<Record<ToolStatus, string>> = {
  running: 'tool-card-live',
  failed: 'tool-card-bad',
  awaiting: 'tool-card-warn',
  blocked: 'tool-card-warn',
};

function RunDetails({ run }: { run: ToolRun }) {
  const args = Object.entries(run.args || {});
  const fileParts = (text: string) => splitPaths(text).flatMap(part => typeof part === 'string' ? [] : [part.path]);
  const files = [...new Set([...fileParts(run.summary || ''), ...args.flatMap(([key, value]) => /path|destination|filename/i.test(key) && /^[A-Za-z]:[\\/][^\r\n]+$/.test(String(value)) ? [String(value)] : fileParts(String(value)))])];
  if (!args.length && !run.summary) return null;
  return (
    <div className="mt-2 space-y-1.5 border-t border-[var(--line)] pt-2">
      {args.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {args.map(([k, v]) => (
            <span key={k} className="arg-chip"><span className="t3">{k}</span> <PathText text={String(v)} short /></span>
          ))}
        </div>
      )}
      {run.summary && <p className="select-text text-meta leading-snug t2"><PathText text={run.summary} /></p>}
      {files.map(path => <FileActions key={path} path={path} />)}
    </div>
  );
}

function SettledRow({ run }: { run: ToolRun }) {
  const [open, setOpen] = useState(false);
  const Icon = CATEGORY_ICON[run.category] || Cpu;
  const st = STATUS[run.status] || STATUS.unknown;
  return (
    <article className={`act-row ${open ? 'act-row-open' : ''}`}>
      <button type="button" className="flex w-full items-center gap-2 text-left" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <Icon className="h-3.5 w-3.5 shrink-0 t3" />
        <span className="min-w-0 flex-1 truncate text-small t2">{run.activity}</span>
        <span className="shrink-0 whitespace-nowrap font-mono text-micro tabular-nums t3">{duration(run.duration)}</span>
        <st.Icon className={`h-3 w-3 shrink-0 ${run.status === 'success' ? 'text-[var(--ok)]' : 't3'}`} aria-label={st.label} />
      </button>
      {open && (
        <>
          <p className="mt-1 flex items-center gap-2 font-mono text-micro t3">
            <span>{run.label}</span><span>·</span><span>{st.label}</span><span className="ml-auto">{clock(run.startedAt)}</span>
          </p>
          <RunDetails run={run} />
        </>
      )}
    </article>
  );
}

function ToolCard({ run }: { run: ToolRun }) {
  const [open, setOpen] = useState(false);
  const Icon = CATEGORY_ICON[run.category] || Cpu;
  const st = STATUS[run.status] || STATUS.unknown;
  const args = Object.entries(run.args || {});
  const expandable = args.length > 0 || !!run.summary;
  // Header (expand target) and status row are separate, so the Cancel button
  // sits on the status line without nesting a button inside a button.
  return (
    <article className={`tool-card ${CARD_TONE[run.status] || ''}`}>
      <button type="button" className="flex w-full min-w-0 items-start gap-2.5 text-left" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <span className="tool-icon"><Icon className="h-3.5 w-3.5" /></span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="micro min-w-0 truncate t2">{run.label}</span>
            {run.background && <span className="chip chip-dim shrink-0">BACKGROUND</span>}
            <span className="ml-auto shrink-0 whitespace-nowrap font-mono text-micro tabular-nums t3">
              {run.status === 'running' ? clock(run.startedAt) : duration(run.duration)}
            </span>
          </span>
          <span className="mt-0.5 block truncate text-small t1">{run.activity}</span>
        </span>
      </button>
      <div className="tool-status-row">
        <span className={`chip ${st.cls}`}>
          <st.Icon className={`h-3 w-3 ${run.status === 'running' ? 'animate-spin' : ''}`} />
          {st.label}
        </span>
        {run.routedTo && <span className="min-w-0 truncate font-mono text-micro t3">→ {run.routedTo}</span>}
        {run.status === 'running' && (
          <button type="button" className="tool-cancel"
            title="Cancel only this Live Activity task"
            aria-label={`Cancel task: ${run.activity}`}
            onClick={() => actions.cancelTask(run.id)}>
            <X className="h-3 w-3" /> Cancel
          </button>
        )}
        {expandable && (
          <button type="button" className={`icon-btn icon-btn-sm shrink-0 ${run.status === 'running' ? '' : 'ml-auto'}`} aria-label={open ? 'Hide details' : 'Show details'} aria-expanded={open} onClick={() => setOpen(o => !o)}>
            <ChevronDown className={`h-3 w-3 transition-transform ${open ? 'rotate-180' : ''}`} />
          </button>
        )}
      </div>
      {open && <RunDetails run={run} />}
    </article>
  );
}

function ActivityPanel() {
  const { jobs, stale } = useDevelopment();
  const currentJob = jobs.find(working) || jobs.find(j => j.state === 'awaiting_approval');
  const link = useHud(s => s.link);
  const tools = useHud(s => s.tools);
  const currentTool = tools.find(t => t.status === 'running' && !['autonomous_development', 'self_improve'].includes(t.tool));

  const running = tools.filter(t => t.status === 'running').length;

  // Only executable work belongs here. Ambient observer events have their own bar.
  const items = useMemo(() => tools.map(tool => ({ t: tool.startedAt, pin: ACTIVE.has(tool.status), tool }))
    .sort((a, b) => Number(b.pin) - Number(a.pin) || b.t - a.t).slice(0, 60), [tools]);

  return (
    <section className="panel flex min-h-0 flex-1 flex-col" aria-label="Live activity">
      <div className="panel-head">
        <span className="micro panel-title">LIVE ACTIVITY</span>
        <span className={`chip ${currentJob?.state === 'awaiting_approval' ? 'chip-warn' : running || (currentJob && !stale) ? 'chip-live' : 'chip-dim'} ml-auto`}>
          {link !== 'online' ? 'OFFLINE' : currentJob && !stale ? currentJob.state === 'awaiting_approval' ? 'REVIEW' : 'DEVELOPING' : running ? `${running} RUNNING` : 'IDLE'}
        </span>
      </div>
      {currentJob ? <TaskProgress name={currentJob.request} value={currentJob.progress} phase={currentJob.phase || currentJob.state} stale={stale || link !== 'online'} /> : currentTool ? <TaskProgress name={currentTool.activity} value={null} phase="Running; this tool does not report measured progress" stale={link !== 'online'} onCancel={() => actions.cancelTask(currentTool.id)} /> : null}
      <div className="custom-scrollbar min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3">
        {items.length === 0 && (
          <p className="px-1 pt-2 text-meta leading-relaxed t3">
            No tasks running. Your requested work appears here; system changes stay in the event bar.
          </p>
        )}
        {items.map(it => SETTLED.has(it.tool.status) ? <SettledRow key={it.tool.id} run={it.tool} /> : <ToolCard key={it.tool.id} run={it.tool} />)}
        <RecentFiles />
      </div>
    </section>
  );
}

export default memo(ActivityPanel);
