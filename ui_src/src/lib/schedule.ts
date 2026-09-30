import { useEffect } from 'react';
import { getState, setState, useHud } from './store';
import { api } from './transport';
import type { ScheduleSnapshot } from './types';

// Keeps store.schedule in step with GET /api/schedule (read-only; creating or
// changing tasks stays with ERA ONE). Refreshes: when the link comes up, every
// POLL_MS, just after the soonest task is due (so a run is reflected at once),
// and on 'era:schedule' — fired by the transport when the scheduled_task tool
// finishes. Mounted once, in App.

const POLL_MS = 30_000;
const AFTER_DUE_MS = 3_000;
const MAX_DUE_WAIT_MS = 6 * 3600_000; // setTimeout range guard

export const SCHEDULE_EVENT = 'era:schedule';

// A backend started before /api/schedule existed answers 404 until it restarts
// (a restart rotates the session token, which reloads the HUD and clears this).
let unsupported = false;

async function load(): Promise<void> {
  if (unsupported) return;
  try {
    setState({ schedule: await api<ScheduleSnapshot>('/api/schedule') });
  } catch (e) {
    if ((e as { status?: number }).status === 404) {
      unsupported = true;
      setState({ schedule: { ok: false, running: false, tasks: [], error: 'restart' } });
      return;
    }
    // Keep the last good snapshot; only report unavailability if we never had one.
    if (!getState().schedule) setState({ schedule: { ok: false, running: false, tasks: [], error: 'unreachable' } });
  }
}

export function useScheduleSync(): void {
  const online = useHud(s => s.link === 'online');
  const soonest = useHud(s =>
    (s.schedule?.tasks || []).reduce<number | null>(
      (m, t) => (t.nextRun !== null && (m === null || t.nextRun < m) ? t.nextRun : m),
      null,
    ),
  );

  useEffect(() => {
    if (!online) return;
    void load();
    const t = window.setInterval(() => void load(), POLL_MS);
    const on = () => void load();
    window.addEventListener(SCHEDULE_EVENT, on);
    return () => { window.clearInterval(t); window.removeEventListener(SCHEDULE_EVENT, on); };
  }, [online]);

  useEffect(() => {
    if (!online || soonest === null) return;
    const wait = soonest * 1000 - Date.now() + AFTER_DUE_MS;
    if (wait <= 0 || wait > MAX_DUE_WAIT_MS) return;
    const t = window.setTimeout(() => void load(), wait);
    return () => window.clearTimeout(t);
  }, [online, soonest]);
}
