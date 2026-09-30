import { memo, useEffect, useState } from 'react';
import NeuralCore from './NeuralCore';
import { useHud } from '../lib/store';
import { STATE_HUE, STATE_LABEL } from '../lib/format';
import type { AgentState } from '../lib/types';

function useReducedMotion(): boolean {
  const [rm, setRm] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const on = () => setRm(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return rm;
}

interface Props {
  nodes: string[];
}

function CoreStage({ nodes }: Props) {
  const link = useHud(s => s.link);
  const agent = useHud(s => s.agent);
  const theme = useHud(s => s.theme);
  const pulse = useHud(s => s.toolPulse);
  const tools = useHud(s => s.tools);
  const reducedMotion = useReducedMotion();

  // With the backend link down nothing below is known: show OFFLINE, truthfully.
  const state: AgentState = link === 'online' ? agent.state : 'offline';
  const { h, s } = STATE_HUE[state];
  const running = tools.filter(t => t.status === 'running' && !t.background);
  const activeCategory = running.length ? running[running.length - 1].category : '';
  const sub = link !== 'online' ? 'Reconnecting to the local backend' : agent.label;

  return (
    <div className="relative min-h-0 flex-1">
      <NeuralCore
        state={state}
        hue={h}
        sat={s}
        light={theme === 'light'}
        nodes={nodes}
        activeCategory={activeCategory}
        toolPulse={pulse.seq}
        reducedMotion={reducedMotion}
      />
      <div className="pointer-events-none absolute inset-x-0 bottom-3 flex flex-col items-center gap-1" aria-live="polite">
        <span className="micro">AGENT STATE</span>
        <span className="state-label" data-state={state}>{STATE_LABEL[state]}</span>
        {sub && sub.toUpperCase() !== STATE_LABEL[state] && (
          <span className="max-w-[28rem] truncate px-4 text-center font-mono text-[11px] t2">{sub}</span>
        )}
      </div>
    </div>
  );
}

export default memo(CoreStage);
