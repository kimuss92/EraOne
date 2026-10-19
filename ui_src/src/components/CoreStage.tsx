import { memo, useMemo } from 'react';
import NeuralCore from './NeuralCore';
import type { CoreNode } from './core/constellation';
import { useHud } from '../lib/store';
import { STATE_HUE, STATE_LABEL } from '../lib/format';
import { tintHue, useLook } from '../lib/appearance';
import type { AgentState } from '../lib/types';
import { useReducedMotion } from '../lib/motion';

interface Props {
  nodes: CoreNode[];
  hero?: boolean;
  paused?: boolean;
}

// Meaning colours never follow the viewer's colour preset.
const SEMANTIC = new Set<AgentState>(['complete', 'error', 'awaiting_approval', 'offline', 'standby', 'interrupted']);

function CoreStage({ nodes, hero = false, paused = false }: Props) {
  const link = useHud(s => s.link);
  const agent = useHud(s => s.agent);
  const theme = useHud(s => s.theme);
  const pulse = useHud(s => s.toolPulse);
  const tools = useHud(s => s.tools);
  const focused = useHud(s => s.drawer !== null);
  const reducedMotion = useReducedMotion();
  const look = useLook();

  // With the backend link down nothing below is known: show OFFLINE, truthfully.
  const state: AgentState = link === 'online' ? agent.state : 'offline';
  const { h: baseHue, s } = STATE_HUE[state];
  const h = tintHue(baseHue, SEMANTIC.has(state));
  const running = tools.filter(t => t.status === 'running' && !t.background);
  const activeCategory = running.length ? running[running.length - 1].category : '';
  // Real usage history: the latest start time per capability category.
  const lastUsed = useMemo(() => {
    const out: Record<string, number> = {};
    for (const t of tools) if (t.category && (out[t.category] ?? 0) < t.startedAt) out[t.category] = t.startedAt;
    return out;
  }, [tools]);
  const sub = link !== 'online' ? 'Reconnecting to the local backend' : agent.label;

  return (
    <div className="era-neural-stage relative min-h-0 flex-1" data-preset={look.preset}>
      <NeuralCore
        hero={hero}
        paused={paused}
        state={state}
        hue={h}
        sat={s}
        light={theme === 'light'}
        nodes={nodes}
        lastUsed={lastUsed}
        activeCategory={activeCategory}
        toolPulse={pulse.seq}
        reducedMotion={reducedMotion}
        focused={focused}
      />
      <div className="era-state-heading pointer-events-none absolute inset-x-0 top-3 flex flex-col items-center gap-1" aria-live="polite">
        {hero && <span className="era-wordmark" data-state={state} aria-label="ERA ONE">ERA<i aria-hidden="true" />ONE</span>}
        <span className="micro">AGENT STATE</span>
        <span className="state-label" data-state={state}>{STATE_LABEL[state]}</span>
        {sub && sub.toUpperCase() !== STATE_LABEL[state] && (
          <span className="max-w-[28rem] truncate px-4 text-center font-mono text-meta t2">{sub}</span>
        )}
      </div>
    </div>
  );
}

export default memo(CoreStage);
