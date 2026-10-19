import { useEffect, useState } from 'react';
import { useReducedMotion } from './motion';

export type CodeRoomPhase = 'off' | 'entering' | 'on' | 'leaving';

export function useCodeTransition(enabled: boolean) {
  const reduced = useReducedMotion();
  const [phase, setPhase] = useState<CodeRoomPhase>(enabled ? 'on' : 'off');
  useEffect(() => {
    if (reduced) { setPhase(enabled ? 'on' : 'off'); return; }
    setPhase(previous => enabled ? previous === 'on' ? 'on' : 'entering' : previous === 'off' ? 'off' : 'leaving');
    const timer = window.setTimeout(() => setPhase(enabled ? 'on' : 'off'), 800);
    return () => window.clearTimeout(timer);
  }, [enabled, reduced]);
  return { phase, reduced };
}
