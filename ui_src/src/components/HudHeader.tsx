import { memo, useEffect, useState } from 'react';
import {
  Code2,
  Command,
  Maximize,
  Minimize,
  Minus,
  MessageSquareText,
  PanelLeft,
  PanelRight,
  Settings,
  ShieldQuestion,
  Square,
  X,
} from 'lucide-react';
import ModelIndicator from './ModelIndicator';
import { useDevelopment, working } from '../lib/development';
import { actions, isQtHost } from '../lib/transport';
import { DOCK_QUERY, setState, togglePanel, useHud, useMedia } from '../lib/store';

function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return (
    <div className="seg">
      <span className="micro !text-[var(--era-blue)]">
        {now.toLocaleDateString([], { weekday: 'short', day: '2-digit', month: 'short' }).toUpperCase()}
      </span>
      <span className="font-mono text-body tabular-nums text-[var(--you)]">
        {now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
      </span>
    </div>
  );
}

function Segment({ label, value, sub, tone = 't1', onClick, title }: {
  label: string;
  value: string;
  sub?: string;
  tone?: string;
  onClick?: () => void;
  title?: string;
}) {
  const body = (
    <>
      <span className="micro">{label}</span>
      <span className={`truncate text-small font-medium ${tone}`}>
        {value}
        {sub && <span className="t3 ml-1.5 font-mono text-micro">{sub}</span>}
      </span>
    </>
  );
  return onClick ? (
    <button type="button" onClick={onClick} title={title} className="seg seg-btn">
      {body}
    </button>
  ) : (
    <div className="seg" title={title}>{body}</div>
  );
}

function useFullscreen(): [boolean, () => void] {
  const [fs, setFs] = useState(Boolean(document.fullscreenElement));
  useEffect(() => {
    const on = () => setFs(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', on);
    return () => document.removeEventListener('fullscreenchange', on);
  }, []);
  const toggle = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void document.documentElement.requestFullscreen?.().catch(() => {});
  };
  return [fs, toggle];
}

interface Props {
  toolCount: number | null;
  voice: { tts: string; stt: string } | null;
}

function HudHeader({ toolCount, voice }: Props) {
  const { jobs, stale } = useDevelopment();
  const pending = jobs.filter(j => j.state === 'awaiting_approval').length;
  const active = !stale && jobs.some(working);
  const link = useHud(s => s.link);
  const rtt = useHud(s => s.rttMs);
  const conn = useHud(s => s.connection);
  const micActive = useHud(s => s.micActive);
  const standby = useHud(s => s.standby);
  const model = useHud(s => s.model);
  const catalog = useHud(s => s.catalog);
  const selected = useHud(s => s.selectedModel);
  const showLeft = useHud(s => s.showLeft);
  const showRight = useHud(s => s.showRight);
  const overlay = useHud(s => s.overlay);
  const approval = useHud(s => s.approval);
  const leftOn = useMedia(DOCK_QUERY.left) ? showLeft : overlay === 'left';
  const rightOn = useMedia(DOCK_QUERY.right) ? showRight : overlay === 'right';
  const [fs, toggleFs] = useFullscreen();

  const linkUp = link === 'online';
  const modelOk = conn.kind === 'connected' || conn.kind === 'local';
  const micLabel = !linkUp ? 'UNKNOWN' : standby ? 'STANDBY' : micActive ? 'MIC ACTIVE' : 'MIC OFF';

  return (
    <header className="hud-header relative z-[var(--z-header)] flex shrink-0 items-center gap-4 px-4">
      <div className="flex items-center gap-3 pr-2" title={linkUp ? 'Connected to the local backend' : 'Local backend unreachable'}>
        <span className={`brand-dot ${linkUp ? '' : 'brand-dot-off'}`} aria-hidden="true" />
        <div className="leading-none">
          <div className="whitespace-nowrap text-title font-semibold tracking-[0.28em] t1">ERA ONE</div>
          {!linkUp && <div className="micro mt-1 text-[var(--bad)]">LINK DOWN</div>}
        </div>
      </div>

      <div className="seg min-w-0">
        <span className="micro">MODEL</span>
        <ModelIndicator model={model} connected={linkUp} catalog={catalog} selected={selected} onSelect={actions.selectModel} />
      </div>
      <Segment
        label="VOICE"
        value={voice && linkUp ? voice.tts : 'N/A'}
        sub={micLabel}
        tone={linkUp && micActive && !standby ? 't1' : 'text-[var(--warn)]'}
        title={voice ? `Speech-to-text: ${voice.stt}` : undefined}
      />
      <div className="hidden lg:block">
        <Segment
          label="TOOLS"
          value={toolCount === null ? 'N/A' : `${toolCount} registered`}
          onClick={() => setState({ drawer: 'capabilities' })}
          title="Open capabilities"
        />
      </div>
      <Segment
        label="CONNECTION"
        value={linkUp ? conn.label || 'Connecting' : 'Reconnecting…'}
        sub={linkUp && rtt !== null ? `${rtt} ms` : undefined}
        tone={!linkUp ? 'text-[var(--bad)]' : modelOk ? 'text-[var(--ok)]' : 'text-[var(--warn)]'}
        title={conn.status || undefined}
      />

      {linkUp && approval && (
        <span className="chip chip-warn shrink-0" role="status" title={`ERA ONE is waiting for your approval: ${approval.title}`}>
          <ShieldQuestion className="h-3 w-3" aria-hidden="true" /> ACTION REQUIRED
        </span>
      )}
      <div className="flex-1" />
      <button className={`dev-trigger ${active ? 'dev-trigger-active' : ''}`} aria-label="Development" title="Inspect source, stage improvements and review verified changes" onClick={() => setState({ drawer: 'development' })}>
        <Code2 className="h-4 w-4" /><span>Development</span>{pending > 0 && <span className="dev-badge">{pending}</span>}
      </button>
      <div className="hidden md:block"><Clock /></div>

      <nav className="flex items-center gap-1" aria-label="HUD controls">
        <button className="icon-btn" title="Live activity panel" aria-label="Live activity panel" aria-pressed={leftOn} onClick={() => togglePanel('left')}>
          <PanelLeft className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Context panel" aria-label="Context panel" aria-pressed={rightOn} onClick={() => togglePanel('right')}>
          <PanelRight className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Conversation history" aria-label="Conversation history" onClick={() => setState({ drawer: 'conversation' })}>
          <MessageSquareText className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Command palette (Ctrl+K)" aria-label="Command palette (Ctrl+K)" onClick={() => setState({ palette: true })}>
          <Command className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Settings" aria-label="Settings" onClick={() => setState({ drawer: 'settings' })}>
          <Settings className="h-4 w-4" />
        </button>
        {!isQtHost && (
          <button className="icon-btn" title={fs ? 'Exit fullscreen' : 'Fullscreen'} aria-label={fs ? 'Exit fullscreen' : 'Fullscreen'} onClick={toggleFs}>
            {fs ? <Minimize className="h-4 w-4" /> : <Maximize className="h-4 w-4" />}
          </button>
        )}
        {isQtHost && (
          <>
            <span className="mx-1 h-5 w-px bg-[var(--line)]" aria-hidden="true" />
            <button className="icon-btn" title="Minimize" aria-label="Minimize" onClick={() => actions.windowControl('minimize')}><Minus className="h-4 w-4" /></button>
            <button className="icon-btn" title="Maximize / restore" aria-label="Maximize / restore" onClick={() => actions.windowControl('maximize')}><Square className="h-3.5 w-3.5" /></button>
            <button className="icon-btn hover:!text-[var(--bad)]" title="Close window" aria-label="Close window" onClick={() => actions.windowControl('close')}><X className="h-4 w-4" /></button>
          </>
        )}
      </nav>
    </header>
  );
}

export default memo(HudHeader);
