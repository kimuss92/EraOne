import { createContext, createElement, useContext, useEffect, useState, type ReactNode } from 'react';
import { api } from './transport';

export type CodeModeState = { enabled: boolean; model: string; models: string[]; tools: string[]; phase: string; session_id: string; revision: number; sequence: number; profile: string; job_id: string; work_id: string; error: string; context: { path: string; sha256: string; truncated: boolean }[] };
const Context = createContext<{ state: CodeModeState | null; stale: boolean }>({ state: null, stale: true });
export const useCodeMode = () => useContext(Context);

export function CodeModeProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<CodeModeState | null>(null);
  const [stale, setStale] = useState(true);
  useEffect(() => {
    let active = true, loading = false, queued = false;
    let controller: AbortController | null = null;
    const load = async () => {
      if (loading) { queued = true; return; }
      loading = true;
      const request = new AbortController(); controller = request;
      const deadline = window.setTimeout(() => request.abort(), 4000);
      try {
        const next = await api<CodeModeState>('/api/codemode', { signal: request.signal });
        if (typeof next.enabled !== 'boolean' || typeof next.phase !== 'string'
          || !Array.isArray(next.models) || !Array.isArray(next.tools) || !Array.isArray(next.context)) throw new Error('Invalid CodeMode state');
        if (active) { setState(next); setStale(false); }
      } catch { if (active) setStale(true); }
      finally { window.clearTimeout(deadline); loading = false; if (queued && active) { queued = false; void load(); } }
    };
    void load();
    const timer = window.setInterval(load, 1500);
    window.addEventListener('era:codemode', load);
    return () => { active = false; controller?.abort(); window.clearInterval(timer); window.removeEventListener('era:codemode', load); };
  }, []);
  return createElement(Context.Provider, { value: { state, stale } }, children);
}
