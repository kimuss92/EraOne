import { useEffect, useState } from 'react';
import { Mic, Music, Volume2 } from 'lucide-react';
import { useHud } from '../lib/store';
import { api } from '../lib/transport';
import { focusView } from '../lib/views';

// Audio route, read live: the microphone and speaker streams Era ACTUALLY
// opened (/api/audio). Until a stream has opened — or on a backend older than
// that endpoint — the row shows the Windows default from the device observer,
// marked "default". Never a guessed device.
interface Route { name: string; pinned: boolean; fallback?: boolean; at: string | null }
// Media is a separate route from Era's voice (core/media_routing.py): the
// remembered media device, and what is ACTUALLY playing where right now.
interface MediaRoute { target: string | null; why: string; playing: { app: string; device: string }[] }
interface AudioRoute { input: Route | null; output: Route | null; media?: MediaRoute }
const POLL_MS = 3000;

function useAudioRoute(online: boolean): AudioRoute | null {
  const [route, setRoute] = useState<AudioRoute | null>(null);
  useEffect(() => {
    if (!online) { setRoute(null); return; }
    let stop = false, t = 0, legacy = false;
    // A backend older than /api/audio (404) still reports the speaker stream in
    // /api/status, so the output row works before Era restarts.
    const fetchRoute = (): Promise<AudioRoute> => legacy
      ? api<{ audioOutput?: { device: string | null; pinned: boolean; fallback: boolean } }>('/api/status')
        .then(s => ({ input: null, output: s.audioOutput?.device ? { name: s.audioOutput.device, pinned: s.audioOutput.pinned, fallback: s.audioOutput.fallback, at: null } : null }))
      : api<AudioRoute>('/api/audio');
    const load = () => {
      if (document.hidden) { t = window.setTimeout(load, POLL_MS); return; }
      fetchRoute()
        .then(r => { if (!stop) { setRoute(r); t = window.setTimeout(load, legacy ? 15000 : POLL_MS); } })
        .catch((e: { status?: number }) => {
          if (stop) return;
          if (e?.status === 404 && !legacy) { legacy = true; load(); return; }
          t = window.setTimeout(load, POLL_MS * 4);
        });
    };
    load();
    return () => { stop = true; window.clearTimeout(t); };
  }, [online]);
  return route;
}

export default function AwarenessPanel() {
  const files = useHud(s => s.fileObservation);
  const processes = useHud(s => s.processObservation);
  const foreground = useHud(s => s.foregroundObservation);
  const network = useHud(s => s.networkObservation);
  const devices = useHud(s => s.deviceObservation);
  const online = useHud(s => s.link === 'online');
  const route = useAudioRoute(online);
  const lastFile = files?.changes[files.changes.length - 1];
  const live = devices && ['ready', 'degraded'].includes(devices.health) ? devices : null;
  const systemDefault = (flow: 'input' | 'output') => {
    const id = live?.processDefaults[flow]?.console;
    return live?.endpoints.find(e => e.id === id && e.flow === flow)?.name ?? null;
  };
  const audio = (flow: 'input' | 'output') => {
    const r = route?.[flow];
    if (r) return { name: r.name, note: r.fallback ? 'fallback' : r.pinned ? 'pinned' : 'in use' };
    const d = systemDefault(flow);
    return d ? { name: d, note: 'default' } : null;
  };
  const mic = audio('input'), out = audio('output');
  const media = (() => {
    const m = route?.media;
    if (!m) return null;
    if (m.playing.length) {
      const devices = [...new Set(m.playing.map(p => p.device))];
      const apps = [...new Set(m.playing.map(p => p.app))].join(', ');
      return { name: devices.length === 1 ? devices[0] : `${devices.length} devices`, note: apps,
        title: m.playing.map(p => `${p.app} → ${p.device}`).join('; ') };
    }
    return m.target ? { name: m.target, note: 'idle' } : { name: 'Follows default', note: m.why };
  })();
  const facts = [
    ['Foreground', foreground?.health === 'ready' ? foreground.foreground?.app : null],
    ['Processes', processes && ['ready', 'degraded'].includes(processes.health) ? processes.processes.length : null],
    ['Network observer', network?.health],
    ['Latest file', lastFile ? `${lastFile.kind}: ${(lastFile.destination || lastFile.path).split(/[\\/]/).pop()}` : null],
  ];
  const device = (Icon: typeof Mic, label: string, d: { name: string; note: string; title?: string } | null) =>
    <div className="era-audio-route"><span><Icon aria-hidden="true" />{label}</span>
      <b title={d ? d.title ?? `${d.name} · ${d.note}` : 'N/A'}>{online && d ? d.name : 'N/A'}</b>
      {online && d && <em>{d.note}</em>}</div>;
  return <div className="era-awareness-facts">
    {device(Mic, 'Era mic', mic)}
    {device(Volume2, 'Era voice', out)}
    {device(Music, 'Media out', media)}
    {facts.map(([label, value]) => <div key={label}><span>{label}</span><b title={String(value ?? '')}>{online ? value ?? 'N/A' : 'N/A'}</b></div>)}
    <button onClick={() => focusView('diagnostics')}>View diagnostics ↗</button>
  </div>;
}
