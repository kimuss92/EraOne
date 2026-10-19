import { useEffect, useState, type CSSProperties } from 'react';
import { useHud } from '../lib/store';
import { api } from '../lib/transport';
import { useReducedMotion } from '../lib/motion';

export type BootPhase = 'waiting' | 'revealing' | 'ready';

export default function BootScreen({ capabilitiesReady, onPhaseChange }: { capabilitiesReady: boolean; onPhaseChange?: (phase: BootPhase) => void }) {
  const reduced = useReducedMotion();
  const link = useHud(s => s.link);
  const snapshot = useHud(s => s.backendStartedAt !== null);
  const [ready, setReady] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [gone, setGone] = useState(false);
  const [health, setHealth] = useState('Waiting for backend');
  useEffect(() => {
    if (dismissed) return;
    let stopped = false;
    let controller: AbortController | null = null;
    let loading = false;
    const poll = async () => {
      if (loading) return;
      loading = true; controller = new AbortController();
      const deadline = window.setTimeout(() => controller?.abort(), 4000);
      try {
        const h = await api<{ status: string }>('/api/health', { signal: controller.signal });
        if (!stopped) { setReady(h.status === 'ready'); setHealth(h.status === 'ready' ? 'Backend ready' : 'Backend initializing'); }
      } catch { if (!stopped) { setReady(false); setHealth('Backend unavailable · retrying'); } }
      finally { loading = false; window.clearTimeout(deadline); }
    };
    void poll();
    const timer = window.setInterval(poll, 1500);
    return () => { stopped = true; controller?.abort(); window.clearInterval(timer); };
  }, [dismissed]);
  useEffect(() => {
    if (link === 'online' && snapshot && capabilitiesReady && ready) setDismissed(true);
  }, [link, snapshot, capabilitiesReady, ready]);
  useEffect(() => {
    if (!dismissed) return;
    if (reduced) { setGone(true); return; }
    // transitionend can be suppressed when Edge is hidden or motion changes.
    const timer = window.setTimeout(() => setGone(true), 1200);
    return () => window.clearTimeout(timer);
  }, [dismissed, reduced]);
  useEffect(() => { onPhaseChange?.(gone ? 'ready' : dismissed ? 'revealing' : 'waiting'); }, [gone, dismissed, onPhaseChange]);
  if (gone) return null;
  const steps: [boolean, string][] = [
    [link === 'online', link === 'online' ? 'Live connection established' : 'Connecting to Era One'],
    [snapshot, snapshot ? 'Session synchronized' : 'Waiting for session state'],
    [capabilitiesReady, capabilitiesReady ? 'Tool registry loaded' : 'Loading capabilities'],
    [ready, health],
  ];
  // Real readiness only: the eclipse ignites one quarter per completed step.
  const done = steps.filter(([ok]) => ok).length;
  return <div className={`era-boot ${dismissed ? 'is-ready' : ''}`} role="status" aria-live="polite" aria-hidden={dismissed} inert={dismissed}
    style={{ '--boot': done / steps.length } as CSSProperties}
    data-complete={done === steps.length}
    onTransitionEnd={e => { if (dismissed && e.target === e.currentTarget && e.propertyName === 'opacity') setGone(true); }}>
    <div className="era-boot-card">
      <div className="era-boot-eclipse" aria-hidden="true"><i /><i /><i /><b /></div>
      <p className="era-eyebrow">PERSONAL INTELLIGENCE</p><h1>ERA ONE</h1>
      <p className="t2">Bringing your workspace online</p>
      <div className="era-boot-progress" aria-hidden="true"><span /></div>
      <ul>{steps.map(([ok, label], i) => <li key={i} data-ready={ok} style={{ '--i': i } as CSSProperties}><span className={ok ? 'is-ready' : ''} aria-hidden="true">{ok ? <svg viewBox="0 0 16 16"><path pathLength="1" d="M3 8.5 6.5 12 13 4" /></svg> : '·'}</span>{label}</li>)}</ul>
      <button className="mini-btn" onClick={() => setDismissed(true)}>Open dashboard while connecting</button>
    </div>
  </div>;
}
