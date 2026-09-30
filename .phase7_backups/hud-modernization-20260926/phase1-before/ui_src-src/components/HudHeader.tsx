import { memo, useEffect, useState } from 'react';
import {
  Command,
  Maximize,
  Minimize,
  Minus,
  MessageSquareText,
  PanelLeft,
  PanelRight,
  Settings,
  Square,
  X,
} from 'lucide-react';
import ModelIndicator from './ModelIndicator';
import { actions, isQtHost } from '../lib/transport';
import { setState, useHud } from '../lib/store';
import { STATE_LABEL } from '../lib/format';

function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return (
    <div className="seg">
      <span className="micro">
        {now.toLocaleDateString([], { weekday: 'short', day: '2-digit', month: 'short' }).toUpperCase()}
      </span>
      <span className="font-mono text-[13px] tabular-nums t1">
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
      <span className={`truncate text-[12px] font-medium ${tone}`}>
        {value}
        {sub && <span className="t3 ml-1.5 font-mono text-[10px]">{sub}</span>}
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
  const agent = useHud(s => s.agent);
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
  const [fs, toggleFs] = useFullscreen();

  const linkUp = link === 'online';
  const modelOk = conn.kind === 'connected' || conn.kind === 'local';
  const micLabel = !linkUp ? 'UNKNOWN' : standby ? 'STANDBY' : micActive ? 'MIC ACTIVE' : 'MIC OFF';

  return (
    <header className="hud-header relative z-30 flex h-14 shrink-0 items-center gap-1 px-3">
      <div className="flex items-center gap-2.5 pr-3">
        <span className={`brand-dot ${linkUp ? '' : 'brand-dot-off'}`} />
        <div className="leading-none">
          <div className="text-[15px] font-semibold tracking-[0.32em] t1">ERA ONE</div>
          <div className="micro mt-1">{linkUp ? STATE_LABEL[agent.state] : 'LINK DOWN'}</div>
        </div>
      </div>

      <div className="hdr-divider" />
      <div className="seg min-w-0">
        <span className="micro">MODEL</span>
        <ModelIndicator model={model} connected={linkUp} catalog={catalog} selected={selected} onSelect={actions.selectModel} />
      </div>
      <div className="hdr-divider" />
      <Segment
        label="VOICE"
        value={voice && linkUp ? voice.tts : 'N/A'}
        sub={micLabel}
        tone={linkUp && micActive && !standby ? 't1' : 'text-[var(--warn)]'}
        title={voice ? `Speech-to-text: ${voice.stt}` : undefined}
      />
      <div className="hdr-divider hidden lg:block" />
      <div className="hidden lg:block">
        <Segment
          label="TOOLS"
          value={toolCount === null ? 'N/A' : `${toolCount} registered`}
          onClick={() => setState({ drawer: 'capabilities' })}
          title="Open capabilities"
        />
      </div>
      <div className="hdr-divider" />
      <Segment
        label="CONNECTION"
        value={linkUp ? conn.label || 'Connecting' : 'Reconnecting…'}
        sub={linkUp && rtt !== null ? `${rtt} ms` : undefined}
        tone={!linkUp ? 'text-[var(--bad)]' : modelOk ? 'text-[var(--ok)]' : 'text-[var(--warn)]'}
        title={conn.status || undefined}
      />

      <div className="flex-1" />
      <div className="hidden md:block"><Clock /></div>
      <div className="hdr-divider hidden md:block" />

      <nav className="flex items-center gap-0.5" aria-label="HUD controls">
        <button className="icon-btn" title="Live activity panel" aria-pressed={showLeft} onClick={() => setState(s => ({ showLeft: !s.showLeft }))}>
          <PanelLeft className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Context panel" aria-pressed={showRight} onClick={() => setState(s => ({ showRight: !s.showRight }))}>
          <PanelRight className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Conversation history" onClick={() => setState({ drawer: 'conversation' })}>
          <MessageSquareText className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Command palette (Ctrl+K)" onClick={() => setState({ palette: true })}>
          <Command className="h-4 w-4" />
        </button>
        <button className="icon-btn" title="Settings" onClick={() => setState({ drawer: 'settings' })}>
          <Settings className="h-4 w-4" />
        </button>
        {!isQtHost && (
          <button className="icon-btn" title={fs ? 'Exit fullscreen' : 'Fullscreen'} onClick={toggleFs}>
            {fs ? <Minimize className="h-4 w-4" /> : <Maximize className="h-4 w-4" />}
          </button>
        )}
        {isQtHost && (
          <>
            <div className="hdr-divider" />
            <button className="icon-btn" title="Minimize" onClick={() => actions.windowControl('minimize')}><Minus className="h-4 w-4" /></button>
            <button className="icon-btn" title="Maximize / restore" onClick={() => actions.windowControl('maximize')}><Square className="h-3.5 w-3.5" /></button>
            <button className="icon-btn hover:!text-[var(--bad)]" title="Close window" onClick={() => actions.windowControl('close')}><X className="h-4 w-4" /></button>
          </>
        )}
      </nav>
    </header>
  );
}

export default memo(HudHeader);
