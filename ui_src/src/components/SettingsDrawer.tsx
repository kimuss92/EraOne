import { useEffect, useState } from 'react';
import Drawer from './Drawer';
import { setState, useHud } from '../lib/store';
import { api } from '../lib/transport';
import { setSoundsEnabled } from '../lib/sounds';
import type { HudSettings } from '../lib/types';

function Toggle({ label, hint, on, onChange, disabled }: { label: string; hint?: string; on: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className="flex items-start gap-3 py-2">
      <span className="min-w-0 flex-1">
        <span className="block text-body t1">{label}</span>
        {hint && <span className="block text-meta leading-snug t3">{hint}</span>}
      </span>
      <button type="button" role="switch" aria-checked={on} disabled={disabled} onClick={() => onChange(!on)} className={`switch ${on ? 'switch-on' : ''}`}>
        <span />
      </button>
    </label>
  );
}

export default function SettingsDrawer() {
  const theme = useHud(s => s.theme);
  const [cfg, setCfg] = useState<HudSettings | null>(null);
  const [city, setCity] = useState('');
  const [msg, setMsg] = useState('');

  useEffect(() => {
    api<HudSettings>('/api/settings').then(c => { setCfg(c); setCity(c.weather_city); }).catch(e => setMsg(String(e.message || e)));
  }, []);

  const save = async (patch: Partial<Omit<HudSettings, 'autostart'>> & { autostart?: boolean; apply_voice?: boolean }) => {
    try {
      const next = await api<HudSettings>('/api/settings', { method: 'POST', body: patch });
      setCfg(next);
      setSoundsEnabled(next.hud_sounds);
      setMsg(next.voice_note || 'Saved.');
      window.dispatchEvent(new Event('era:settings'));
    } catch (e) {
      setMsg(String((e as Error).message || e));
    }
  };

  const task = cfg?.autostart?.scheduled_task;
  return (
    <Drawer title="SETTINGS">
      <div className="space-y-1 px-4 py-3">
        {!cfg && !msg && <p className="text-small t3">Loading…</p>}
        {cfg && (
          <>
            <h3 className="micro pt-1 t2">APPEARANCE</h3>
            <Toggle label="Light theme" hint="Also switchable by voice: “light mode” / “dark mode”." on={theme === 'light'} onChange={v => setState({ theme: v ? 'light' : 'dark' })} />
            <Toggle label="HUD sounds" hint="Subtle cues for wake, tool completion, errors and replies. Era's voice is separate." on={cfg.hud_sounds} onChange={v => save({ hud_sounds: v })} />

            <h3 className="micro pt-3 t2">VOICE</h3>
            <label className="block py-2">
              <span className="block text-body t1">Gemini voice</span>
              <span className="block pb-2 text-meta leading-snug t3">
                The 30 voices Gemini Live can speak with. A new choice applies right away (or as soon as Era finishes speaking).
              </span>
              <select
                value={cfg.gemini_voice || ''}
                onChange={e => void save({ gemini_voice: e.target.value, apply_voice: true })}
                className="field w-full px-2 py-1.5 text-body t1 outline-none"
                aria-label="Gemini voice"
              >
                {(['male', 'female', 'neutral'] as const).map(g => {
                  const list = (cfg.gemini_voices || []).filter(v => (v.gender || 'neutral') === g);
                  if (!list.length) return null;
                  return (
                    <optgroup key={g} label={g === 'male' ? 'Male' : g === 'female' ? 'Female' : 'Neutral'}>
                      {list.map(v => <option key={v.name} value={v.name}>{v.name} — {v.style}</option>)}
                    </optgroup>
                  );
                })}
              </select>
              <span className="block pt-1 text-meta t3">
                {cfg.gemini_voice_note || (cfg.gemini_voice_active ? `Active: ${cfg.gemini_voice_active}` : '')}
              </span>
            </label>

            <h3 className="micro pt-3 t2">WEATHER</h3>
            <form className="flex gap-2 py-2" onSubmit={e => { e.preventDefault(); void save({ weather_city: city }); }}>
              <input value={city} onChange={e => setCity(e.target.value)} className="field min-w-0 flex-1 px-2 py-1.5 text-body t1 outline-none" aria-label="Weather location" />
              <button type="submit" className="mini-btn">Save</button>
            </form>

            <h3 className="micro pt-3 t2">STARTUP</h3>
            <Toggle label="Open the HUD when ERA ONE starts on its own" hint="At logon or after `python main.py`. Never after a crash-restart." on={cfg.open_hud_on_start} onChange={v => save({ open_hud_on_start: v })} />
            <Toggle label="App window" hint="Open the HUD as a Chromium app window (no tabs or address bar) instead of a browser tab." on={cfg.app_mode} onChange={v => save({ app_mode: v })} />
            <Toggle
              label="Launch ERA ONE when Windows starts"
              hint={task ? `A legacy scheduled task (EraOneSupervisor) also exists — state: ${task}. Manage it with register_era_supervisor.ps1.` : 'Adds a shortcut to your Startup folder. Off by default.'}
              on={Boolean(cfg.autostart?.startup_shortcut)}
              onChange={v => save({ autostart: v })}
            />
          </>
        )}
        {msg && <p className="pt-2 text-meta t3">{msg}</p>}
      </div>
    </Drawer>
  );
}
