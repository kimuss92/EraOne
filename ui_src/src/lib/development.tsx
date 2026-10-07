import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from './transport';
import { setState } from './store';
import type { DrawerId } from './types';
export type Widget = { id: string; kind: string; label: string; value?: string | number; series?: number[]; binding?: 'cpu_percent' | 'ram_percent' | 'development'; action?: { drawer: DrawerId } };
export type Scene = { revision: number; bundle?: string; layout: { theme: 'light' | 'dark'; left: boolean; right: boolean; columns: number; accent: string; density: string }; widgets: Widget[] };
export type Job = { id: string; request: string; state: string; phase?: string; progress: number; attempt?: number; plan: string; error?: string; approval: string; reload: boolean; requires_restart?: boolean; files: { path: string; diff: string }[]; checks: Record<string, unknown>; updated_at?: number };
const Context = createContext<{ jobs: Job[]; scene: Scene | null; stale: boolean; refresh: () => void }>({ jobs: [], scene: null, stale: true, refresh: () => {} });
export const useDevelopment = () => useContext(Context);
export const working = (j: Job) => ['planning', 'repairing', 'verifying', 'activating'].includes(j.state);
export function DevelopmentProvider({ children }: { children: ReactNode }) {
  const [jobs, jobsSet] = useState<Job[]>([]), [scene, sceneSet] = useState<Scene | null>(null), [stale, staleSet] = useState(true);
  const bundle = useRef('');
  useEffect(() => {
    let active = true, loading = false, queued = false;
    const load = async () => {
      if (loading) { queued = true; return; } loading = true;
      try {
        const [next, result] = await Promise.all([api<Scene>('/api/visual'), api<{ jobs: Job[] }>('/api/development')]);
        if (!active) return;
        if (!Array.isArray(result.jobs) || !next.layout || !Array.isArray(next.widgets)) throw new Error('Invalid development snapshot');
        const safeJobs = result.jobs.filter(j => j && typeof j.id === 'string' && typeof j.request === 'string' && typeof j.state === 'string').map(j => ({ ...j, progress: Number.isFinite(j.progress) ? j.progress : 0, files: Array.isArray(j.files) ? j.files : [], checks: j.checks && typeof j.checks === 'object' ? j.checks : {} }));
        jobsSet(safeJobs); sceneSet(next); staleSet(false);
        if (bundle.current && next.bundle && bundle.current !== next.bundle && safeJobs.some(j => j.state === 'activated' && j.reload)) { window.location.reload(); return; }
        bundle.current = next.bundle || '';
      } catch { if (active) staleSet(true); }
      finally { loading = false; if (queued && active) { queued = false; void load(); } }
    };
    void load(); const timer = window.setInterval(load, 3000);
    window.addEventListener('era:visual', load); window.addEventListener('era:development', load);
    return () => { active = false; clearInterval(timer); window.removeEventListener('era:visual', load); window.removeEventListener('era:development', load); };
  }, []);
  useEffect(() => {
    if (!scene || scene.revision === 0) return;
    setState({ theme: scene.layout.theme, showLeft: scene.layout.left, showRight: scene.layout.right });
    document.documentElement.style.setProperty('--visual-accent', scene.layout.accent);
  }, [scene?.revision]);
  return <Context.Provider value={{ jobs, scene, stale, refresh: () => window.dispatchEvent(new Event('era:development')) }}>{children}</Context.Provider>;
}
