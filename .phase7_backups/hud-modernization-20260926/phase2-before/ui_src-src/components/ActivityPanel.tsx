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
  Radio,
  Route,
  ShieldQuestion,
  Sparkles,
  Workflow,
  Zap,
} from 'lucide-react';
import { useHud } from '../lib/store';
import { clock, duration } from '../lib/format';
import type { TimelineItem, ToolRun, ToolStatus } from '../lib/types';

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
  awaiting: { label: 'AWAITING CONFIRMATION', cls: 'chip-warn', Icon: ShieldQuestion },
  unknown: { label: 'NO RESULT', cls: 'chip-warn', Icon: CircleDashed },
};

const TL_ICON: Record<string, typeof Globe> = {
  routing: Route,
  connection: Radio,
  wake: Zap,
  interrupt: Hand,
  error: AlertTriangle,
};

function ToolCard({ run }: { run: ToolRun }) {
  const [open, setOpen] = useState(false);
  const Icon = CATEGORY_ICON[run.category] || Cpu;
  const st = STATUS[run.status] || STATUS.unknown;
  const args = Object.entries(run.args || {});
  return (
    <article className={`tool-card ${run.status === 'running' ? 'tool-card-live' : ''}`}>
      <button type="button" className="flex w-full items-start gap-2.5 text-left" onClick={() => setOpen(o => !o)}>
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
          <span className="mt-1.5 flex items-center gap-2">
            <span className={`chip ${st.cls}`}>
              <st.Icon className={`h-3 w-3 ${run.status === 'running' ? 'animate-spin' : ''}`} />
              {st.label}
            </span>
            {run.routedTo && <span className="truncate font-mono text-micro t3">→ {run.routedTo}</span>}
            {(args.length > 0 || run.summary) && (
              <ChevronDown className={`ml-auto h-3 w-3 t3 transition-transform ${open ? 'rotate-180' : ''}`} />
            )}
          </span>
        </span>
      </button>
      {open && (
        <div className="mt-2 space-y-1.5 border-t border-[var(--line)] pt-2">
          {args.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {args.map(([k, v]) => (
                <span key={k} className="arg-chip"><span className="t3">{k}</span> {String(v)}</span>
              ))}
            </div>
          )}
          {run.summary && <p className="select-text text-meta leading-snug t2">{run.summary}</p>}
        </div>
      )}
    </article>
  );
}

function TimelineRow({ item }: { item: TimelineItem }) {
  const Icon = TL_ICON[item.kind] || Radio;
  return (
    <div className={`tl-row tl-${item.tone}`}>
      <Icon className="h-3 w-3 shrink-0" />
      <span className="min-w-0 flex-1 truncate">{item.label}</span>
      {item.duration !== undefined && <span className="font-mono tabular-nums">{duration(item.duration)}</span>}
      <span className="font-mono tabular-nums t3">{clock(item.ts)}</span>
    </div>
  );
}

function ActivityPanel() {
  const tools = useHud(s => s.tools);
  const timeline = useHud(s => s.timeline);
  const running = tools.filter(t => t.status === 'running').length;

  // Newest first; tool cards and timeline rows interleaved by time.
  const items = useMemo(() => {
    const rows: ({ t: number; tool: ToolRun } | { t: number; tl: TimelineItem })[] = [
      ...tools.map(tool => ({ t: tool.startedAt, tool })),
      ...timeline.map(tl => ({ t: tl.ts, tl })),
    ];
    return rows.sort((a, b) => b.t - a.t).slice(0, 60);
  }, [tools, timeline]);

  return (
    <section className="panel flex min-h-0 flex-1 flex-col" aria-label="Live activity">
      <div className="panel-head">
        <span className="micro panel-title">LIVE ACTIVITY</span>
        <span className={`chip ${running ? 'chip-live' : 'chip-dim'} ml-auto`}>
          {running ? `${running} RUNNING` : 'IDLE'}
        </span>
      </div>
      <div className="custom-scrollbar min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
        {items.length === 0 && (
          <p className="px-1 pt-2 text-meta leading-relaxed t3">
            No activity yet. Tool calls, routing and link events appear here as ERA ONE performs them.
          </p>
        )}
        {items.map(it => ('tool' in it ? <ToolCard key={it.tool.id} run={it.tool} /> : <TimelineRow key={it.tl.id} item={it.tl} />))}
      </div>
    </section>
  );
}

export default memo(ActivityPanel);
