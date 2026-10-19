import { useEffect, useRef } from 'react';
import { createEclipse } from './core/eclipse';
import { beginFrame } from './core/sprites';
import { live, useHud } from '../lib/store';
import { useCodeMode } from '../lib/codemode';
import type { AgentState } from '../lib/types';
import { startCanvasLoop } from '../lib/canvasLoop';

export default function CodeModeOrb({ reduced }: { reduced: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const { state, stale } = useCodeMode();
  const cpu = useHud(s => s.link === 'online' ? s.metrics.cpu_percent : null);
  const agent = useHud(s => s.agent.state);
  const link = useHud(s => s.link);
  const running = useHud(s => s.tools.filter(t => t.status === 'running').length);
  const busy = !!state && ['context', 'responding', 'verifying'].includes(state.phase);
  const current = useRef({ reduced, busy, cpu, agent, running, stale, link });
  current.current = { reduced, busy, cpu, agent, running, stale, link };
  useEffect(() => {
    const c = canvas.current!, ctx = c.getContext('2d');
    if (!ctx) return;
    const eclipse = createEclipse(() => .5);
    let last = 0, time = 0;
    const draw = (now: number) => {
      const v = current.current;
      const dt = Math.min(.1, (now - last) / 1000); last = now;
      if (!v.reduced && v.link === 'online') time += dt * (v.busy || v.running ? 1.4 : .35);
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      if (c.width !== Math.round(140 * dpr)) { c.width = Math.round(140 * dpr); c.height = Math.round(140 * dpr); }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, 140, 140); beginFrame();
      const st: AgentState = v.link !== 'online' || v.stale ? 'offline' : v.agent === 'speaking' || v.agent === 'listening' ? v.agent : v.busy ? 'thinking' : v.running ? 'tool_running' : 'idle';
      const freshVoice = v.link === 'online' && v.agent === 'speaking';
      const f = {
        ctx, cx: 70, cy: 70, R: 48, hue: 262, sat: 75, light: false,
        inten: v.stale ? .3 : v.busy || v.running ? .9 : .6,
        time, dt, motion: v.reduced ? 0 : 1, st,
        amp: freshVoice ? live.amplitude : 0,
        mic: v.link === 'online' && now - live.micLevelAt < 1000 ? live.micLevel : 0,
        eqAt: (i: number) => freshVoice ? live.bands[i] || 0 : 0,
        eqOn: freshVoice, eqBands: 8, glowK: 1.25, density: 'low' as const,
      };
      eclipse.halo(f);
      eclipse.body(f, () => {
        // Orthogonal circuits replace the main orb's random neural network.
        ctx.save(); ctx.beginPath(); ctx.arc(70,70,43,0,Math.PI*2); ctx.clip();
        for (let i = 0; i < 6; i++) {
          const y = 39 + i * 12, offset = i % 2 ? 9 : -9;
          ctx.beginPath(); ctx.moveTo(27,y); ctx.lineTo(52,y); ctx.lineTo(52+offset,y-5); ctx.lineTo(113,y-5);
          ctx.strokeStyle = i % 2 ? '#8ac8ed48' : '#b68cff45'; ctx.lineWidth = .7; ctx.stroke();
          const t = (time * (v.busy || v.running ? 30 : 8) + i * 17) % 86;
          const x = 27 + t;
          ctx.fillStyle = i % 2 ? '#acdfff' : '#d4b5ff';
          ctx.fillRect(x, x < 52 ? y - 1 : y - 6, 2, 2);
        }
        // The outer arc is real CPU load, omitted when metrics are unknown.
        ctx.restore();
      });
      if (v.cpu !== null && Number.isFinite(v.cpu)) {
        ctx.beginPath(); ctx.arc(70,70,59,-Math.PI/2,-Math.PI/2 + Math.PI*2*Math.max(0,Math.min(100,v.cpu))/100);
        ctx.strokeStyle = '#c8a9ff'; ctx.lineWidth = 2; ctx.stroke();
      }
    };
    return startCanvasLoop(c,draw,() => current.current.reduced ? 250 : current.current.busy || current.current.running ? 50 : 100);
  }, []);
  const phase = stale ? 'STALE' : link !== 'online' ? 'OFFLINE' : busy ? 'PROCESSING' : running ? `${running} ACTIVE` : 'READY';
  return <div className="cm-orb" role="img" aria-label={`Agent process: ${phase}. CPU ${cpu === null ? 'unavailable' : `${Math.round(cpu)} percent`}`}>
    <canvas ref={canvas} aria-hidden="true" />
    <div className="cm-orb-label">{phase}<small>CPU {cpu === null ? '—' : `${Math.round(cpu)}%`}</small></div>
  </div>;
}
