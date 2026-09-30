import { useEffect, useState, type ReactNode } from 'react';
import Drawer from './Drawer';
import { live, useHud } from '../lib/store';
import { api } from '../lib/transport';
import { ago, clock, duration, prettyModel, uptime } from '../lib/format';

// Everything here is measured or read from the running backend
// (/api/diagnostics), plus two numbers only the browser can measure: the HUD's
// real render rate and the WebSocket round trip. Nothing is estimated.

interface Diag {
  status: Record<string, any>;
  latency: Record<string, any>;
  failures: { ts: number; where: string; message: string }[];
  recentEvents: { ts: number; type: string; label?: string; message?: string; toolName?: string }[];
  websocket: { clients: number };
  process: { pid: number; python: string; os: string };
}

function Row({ k, v, tone = 't1' }: { k: string; v: ReactNode; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="micro">{k}</span>
      <span className={`truncate text-right font-mono text-meta ${tone}`}>{v}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-b border-[var(--line)] px-4 py-3">
      <h3 className="micro mb-2 t2">{title}</h3>
      {children}
    </section>
  );
}

const yes = (b: unknown) => (b === null || b === undefined ? 'N/A' : b ? 'YES' : 'NO');

export default function DiagnosticsDrawer() {
  const [d, setD] = useState<Diag | null>(null);
  const [err, setErr] = useState('');
  const [fps, setFps] = useState(live.fps);
  const rtt = useHud(s => s.rttMs);
  const link = useHud(s => s.link);

  useEffect(() => {
    const load = () => api<Diag>('/api/diagnostics').then(x => { setD(x); setErr(''); }).catch(e => setErr(String(e.message || e)));
    load();
    const t = window.setInterval(() => { load(); setFps(live.fps); }, 2500);
    return () => window.clearInterval(t);
  }, []);

  const s = d?.status || {};
  const h = s.health || {};
  const lat = d?.latency || {};
  return (
    <Drawer title="DIAGNOSTICS">
      {err && <p className="px-4 pt-3 text-meta text-[var(--warn)]">{err}</p>}
      <Section title="MODEL & PROVIDERS">
        <Row k="VOICE SESSION MODEL" v={prettyModel(s.model?.voiceSession)} />
        <Row k="LAST ROUTED MODEL" v={s.model?.routed?.label || 'N/A'} />
        <Row k="MODEL LINK" v={s.connection?.status || 'N/A'} tone={s.connection?.kind === 'connected' ? 'text-[var(--ok)]' : 'text-[var(--warn)]'} />
        <Row k="STT" v={s.voice?.stt || 'N/A'} />
        <Row k="TTS" v={s.voice?.tts || 'N/A'} />
        <Row k="VOICE MODE" v={s.voice?.voiceMode || 'N/A'} />
      </Section>
      <Section title="AUDIO">
        <Row k="MICROPHONE" v={String(s.mic || 'N/A').toUpperCase()} tone={s.mic === 'active' ? 'text-[var(--ok)]' : 'text-[var(--warn)]'} />
        <Row k="OWNER VOICE LOCK" v={yes(s.voiceLock)} />
        <Row k="AUDIO OUTPUT" v={s.audioOutput?.device || 'N/A'} />
        <Row k="OUTPUT PINNED" v={yes(s.audioOutput?.pinned)} />
      </Section>
      <Section title="LINKS">
        <Row k="HUD WEBSOCKET" v={link === 'online' ? `ONLINE · ${rtt ?? '—'} ms RTT` : 'RECONNECTING'} tone={link === 'online' ? 'text-[var(--ok)]' : 'text-[var(--bad)]'} />
        <Row k="HUD CLIENTS" v={d?.websocket.clients ?? 'N/A'} />
        <Row k="LOCAL SERVER" v={h.port ? `127.0.0.1:${h.port}` : 'N/A'} />
        <Row k="INTERNET" v={s.internet === null || s.internet === undefined ? 'CHECKING' : s.internet ? 'ONLINE' : 'OFFLINE'} tone={s.internet === false ? 'text-[var(--bad)]' : 't1'} />
        <Row k="BACKEND" v={h.agent ? `${String(h.agent).toUpperCase()} · pid ${h.pid}` : 'N/A'} />
        <Row k="BACKEND UPTIME" v={uptime(h.uptime_s)} />
        <Row k="BUILD" v={h.build || 'N/A'} />
        <Row k="RUNTIME" v={d ? `Python ${d.process.python} · ${d.process.os}` : 'N/A'} />
      </Section>
      <Section title="TOOLS">
        <Row k="REGISTERED" v={s.tools?.count ?? 'N/A'} />
        <Row k="ACTIVE" v={s.tools?.active?.length ? s.tools.active.map((t: any) => t.label).join(', ') : 'none'} />
      </Section>
      <Section title="LATENCY (MEASURED)">
        <Row k="TRANSCRIPTION SPAN" v={`${duration(lat.transcriptionMs)} · avg ${duration(lat.transcriptionAvgMs)}`} />
        <Row k="TIME TO FIRST TOKEN" v={`${duration(lat.ttftMs)} · avg ${duration(lat.ttftAvgMs)}`} />
        <Row k="TIME TO FIRST AUDIO" v={`${duration(lat.firstAudioMs)} · avg ${duration(lat.firstAudioAvgMs)}`} />
        <Row k="RESPONSE DURATION" v={`${duration(lat.responseMs)} · avg ${duration(lat.responseAvgMs)}`} />
        {Object.entries(lat.tools || {}).slice(-6).map(([k, v]) => <Row key={k} k={k.toUpperCase()} v={duration(v as number)} />)}
      </Section>
      <Section title="HUD">
        <Row k="RENDER RATE" v={`${fps} fps`} />
        <Row k="HOST" v={window.location.protocol.startsWith('http') ? 'Browser (localhost)' : 'Legacy desktop window'} />
      </Section>
      <Section title="RECENT FAILURES">
        {(d?.failures || []).length === 0 && <p className="text-meta t3">None recorded this session.</p>}
        {(d?.failures || []).slice().reverse().map((f, i) => (
          <div key={i} className="py-1 text-meta">
            <span className="font-mono t3">{clock(f.ts)} · {f.where}</span>
            <p className="select-text text-[var(--warn)]">{f.message}</p>
          </div>
        ))}
      </Section>
      <Section title="RECENT EVENTS">
        {(d?.recentEvents || []).slice().reverse().slice(0, 30).map((e, i) => (
          <div key={i} className="flex gap-2 py-0.5 font-mono text-meta">
            <span className="shrink-0 t3">{ago(e.ts)}</span>
            <span className="shrink-0 ac-text">{e.type}</span>
            <span className="truncate t2">{e.toolName || e.label || e.message || ''}</span>
          </div>
        ))}
      </Section>
    </Drawer>
  );
}
