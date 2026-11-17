import { useEffect, useState } from 'react';
import { api } from '../lib/transport';
import { setState } from '../lib/store';

type Field = { key: string; label: string; kind: string; value: string; configured: boolean };
type Config = { fields: Field[]; dependencies: Record<string, boolean>; note: string };
type Launcher = { floating_icon: boolean; desktop_tray: boolean; open_hud_on_start: boolean; floating_magnet?: boolean };
type Companion = { running: boolean; url: string; addresses?: string[]; devices: { id: string; name: string }[]; limitations: string };

export default function EmpowerSettings() {
  const [config, setConfig] = useState<Config | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [phone, setPhone] = useState<Companion | null>(null);
  const [host, setHost] = useState('');
  const [port, setPort] = useState('8766');
  const [pair, setPair] = useState<{ url: string; qr: string } | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [launcher, setLauncher] = useState<Launcher | null>(null);
  const [goal, setGoal] = useState('');
  const [name, setName] = useState('');

  useEffect(() => {
    if (!pair) return;
    const timer = window.setTimeout(() => { setPair(null); setMessage('Pairing link expired. Create a new one if needed.'); }, 120000);
    return () => window.clearTimeout(timer);
  }, [pair]);

  useEffect(() => {
    api<Config>('/api/empower').then(setConfig).catch(e => setMessage(e.message));
    api<Launcher>('/api/launcher').then(setLauncher).catch(() => setLauncher(null));
    api<Companion>('/api/companion').then(p => { setPhone(p); setHost(p.addresses?.[0] || '127.0.0.1'); }).catch(e => setMessage(e.message));
  }, []);

  const perform = async (operation: () => Promise<void>) => {
    setBusy(true); setMessage('');
    try { await operation(); } catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  };
  const save = (key: string, value: string) => perform(async () => {
    const next = await api<Config>('/api/empower/settings', { method: 'POST', body: { [key]: value } });
    setConfig(next); setDraft(d => { const copy = { ...d }; delete copy[key]; return copy; });
    setMessage('Saved. ' + next.note);
  });
  const phoneAction = (action: string, extra = {}) => perform(async () => {
    const result = await api<Companion & { url: string; qr: string }>('/api/companion', { method: 'POST', body: { action, ...extra } });
    if (action === 'pair') { setPair(result); setMessage('Scan within two minutes. The link works once.'); }
    else { setPair(null); setPhone(await api<Companion>('/api/companion')); setMessage(action === 'start' ? 'Companion started on the selected address.' : 'Updated.'); }
  });

  return <div className="space-y-4 border-t border-white/10 pt-4 mt-4">
    <h3 className="micro t2">CAPABILITIES & CONNECTIONS</h3>
    <p className="text-meta t3">Create documents, inspect apps, build skills and connect your phone. Existing voice, desktop and memory controls stay available.</p>
    <details className="rounded-lg border border-white/10 p-3" open>
      <summary className="text-body t1 cursor-pointer">Desktop launcher</summary>
      <p className="text-meta t3 py-2">A movable icon above your windows, with a tray menu for opening the workspace, startup, restart and quit.</p>
      <div className="flex gap-2 flex-wrap items-center" role="group" aria-label="Floating icon">{(['show', 'hide'] as const).map(action => { const on = launcher ? (action === 'show') === (launcher.floating_icon && launcher.desktop_tray) : false; return <button key={action} className="mini-btn mini-btn-choice" aria-pressed={on} disabled={busy} onClick={() => void perform(async () => { await api('/api/launcher', { method: 'POST', body: { action } }); setLauncher(await api<Launcher>('/api/launcher')); setMessage(action === 'show' ? 'Floating icon on. It also comes back by itself when you close the workspace.' : 'Floating icon hidden; tray menu stays available. It returns when you close the workspace.'); })}>{on && <span aria-hidden="true">✓</span>}{action === 'show' ? 'Show floating icon' : 'Hide floating icon'}</button>; })}</div>
      <div className="flex gap-2 flex-wrap items-center pt-2" role="group" aria-label="Magnet to screen edge">{(['magnet_on', 'magnet_off'] as const).map(action => { const on = launcher ? (action === 'magnet_on') === (launcher.floating_magnet ?? true) : false; return <button key={action} className="mini-btn mini-btn-choice" aria-pressed={on} disabled={busy} onClick={() => void perform(async () => { await api('/api/launcher', { method: 'POST', body: { action } }); setLauncher(await api<Launcher>('/api/launcher')); setMessage(action === 'magnet_on' ? 'Magnet on: the icon snaps to the nearest screen edge when you drop it.' : 'Magnet off: the icon stays exactly where you drop it.'); })}>{on && <span aria-hidden="true">✓</span>}{action === 'magnet_on' ? 'Magnet to edge' : 'Free position'}</button>; })}</div>
      <p className="text-meta t3 pt-2">{launcher ? `Now: icon ${launcher.floating_icon && launcher.desktop_tray ? 'shown' : 'hidden'} · magnet ${(launcher.floating_magnet ?? true) ? 'on' : 'off'}` : 'Status unavailable.'}</p>
    </details>
    <details className="rounded-lg border border-white/10 p-3">
      <summary className="text-body t1 cursor-pointer">Services, vision & document folder</summary>
      <p className="text-meta t3 py-2">Stored secrets are never sent back to this panel. Saving a blank key clears it. Connection tests report the service response.</p>
      {config?.fields.map(field => <form key={field.key} className="py-2" onSubmit={e => { e.preventDefault(); void save(field.key, draft[field.key] ?? field.value); }}>
        <label className="block text-meta t2" htmlFor={'empower-' + field.key}>{field.label}{field.kind === 'secret' ? (field.configured ? ' · configured' : ' · not configured') : ''}</label>
        <div className="flex gap-2 mt-1"><input id={'empower-' + field.key} type={field.kind === 'secret' ? 'password' : 'text'} autoComplete="off" value={draft[field.key] ?? field.value} onChange={e => setDraft(d => ({ ...d, [field.key]: e.target.value }))} placeholder={field.kind === 'secret' && field.configured ? 'Enter a replacement key' : ''} className="field min-w-0 flex-1 px-2 py-1.5 text-body t1 outline-none" /><button className="mini-btn" disabled={busy || !(field.key in draft)}>Save</button></div>
      </form>)}
      <div className="flex flex-wrap gap-2 pt-2">{['gemini', 'ollama', 'home_assistant'].map(service => <button className="mini-btn" key={service} disabled={busy} onClick={() => void perform(async () => { const result = await api<{ ok: boolean; message: string }>('/api/empower/test', { method: 'POST', body: { service } }); setMessage((result.ok ? 'Connected. ' : 'Not connected. ') + result.message); })}>Test {service.replace(/_/g, ' ')}</button>)}</div>
      <p className="text-meta t3 pt-2">Use “look at my screen” for existing vision. No continuous camera or clipboard monitoring is enabled here.</p>
    </details>
    <details className="rounded-lg border border-white/10 p-3">
      <summary className="text-body t1 cursor-pointer">Era Connect · iPhone & Android</summary>
      <p className="text-meta t3 py-2">{phone?.limitations || 'Loading companion status…'}</p>
      <label className="block text-meta t2">PC network address<select value={host} onChange={e => setHost(e.target.value)} className="field w-full my-1 p-2">{(phone?.addresses || [host]).map(a => <option key={a}>{a}</option>)}</select></label>
      <label className="block text-meta t2">Port<input type="number" min="1024" max="65535" value={port} onChange={e => setPort(e.target.value)} className="field w-full my-1 p-2" /></label>
      <div className="flex flex-wrap gap-2 py-2">
        <button className="mini-btn" disabled={busy || !phone} onClick={() => void phoneAction(phone?.running ? 'stop' : 'start', { host, port: Number(port) })}>{phone?.running ? 'Stop companion' : 'Start on trusted network'}</button>
        <button className="mini-btn" disabled={busy || !phone?.running} onClick={() => void phoneAction('pair')}>Pair a phone</button>
      </div>
      {phone?.running && <p className="text-meta t2 break-all">Listening: {phone.url}</p>}
      {pair && <div className="py-3"><img src={pair.qr} alt="One-use phone pairing QR code; expires in two minutes" width="220" height="220" /><a href={pair.url} target="_blank" rel="noreferrer" className="text-meta t2 underline break-all">Open pairing page</a><p className="text-meta t3">Scan with your phone camera on the same trusted network. In Safari, add the page to your Home Screen. Keep this link private.</p></div>}
      {phone?.devices.map(device => <div key={device.id} className="flex items-center justify-between gap-2 py-2"><span className="text-body t1">{device.name}</span><button className="mini-btn" disabled={busy} onClick={() => void phoneAction('revoke', { id: device.id })}>Revoke</button></div>)}
    </details>
    <details className="rounded-lg border border-white/10 p-3">
      <summary className="text-body t1 cursor-pointer">Skill Crucible</summary>
      <p className="text-meta t3 py-2">Describe a reusable skill. Era stages code and tests; review the result in Development before activating it.</p>
      <input aria-label="Skill name" placeholder="Skill name, e.g. invoice_summary" value={name} onChange={e => setName(e.target.value)} className="field w-full p-2 mb-2" />
      <textarea aria-label="Skill goal" placeholder="What should the skill do, and how should success be checked?" value={goal} maxLength={4000} onChange={e => setGoal(e.target.value)} className="field w-full min-h-24 p-2" />
      <div className="flex gap-2 py-2"><button className="mini-btn" disabled={busy || !/^[a-z][a-z0-9_]{2,47}$/.test(name) || goal.trim().length < 10} onClick={() => void perform(async () => { const result = await api<{ accepted: boolean }>('/api/message', { method: 'POST', body: { text: `Create a reusable skill using the Skill Crucible. Name: ${name}. Goal: ${goal}` } }); setMessage(result.accepted ? 'Request sent to Era. Follow generation and test results in Development.' : 'Era did not accept the request.'); })}>Ask Era to build skill</button><button className="mini-btn" onClick={() => setState({ drawer: 'development' })}>Development</button></div>
    </details>
    <details className="rounded-lg border border-white/10 p-3">
      <summary className="text-body t1 cursor-pointer">Tool readiness & examples</summary>
      <div className="grid grid-cols-2 gap-2 py-2">{Object.entries(config?.dependencies || {}).map(([label, ready]) => <div key={label} className="text-meta"><span className={ready ? 'text-emerald-400' : 'text-amber-400'}>{ready ? '● Ready' : '○ Missing'}</span><span className="block t2">{label}</span></div>)}</div>
      <ul className="text-meta t3 list-disc pl-4 space-y-2"><li>Create a budget workbook with formulas and a bar chart.</li><li>Build a presentation with speaker notes and save it to my document folder.</li><li>Inspect this program or source folder without running it.</li><li>List memory-heavy processes, then pause the one I choose.</li><li>Show the current ISS position or recent earthquakes.</li><li>Guide me through DHT11 wiring for Arduino Uno or ESP32.</li><li>List my Home Assistant devices.</li></ul>
    </details>
    {message && <p role="status" className="text-meta t2 whitespace-pre-wrap">{message}</p>}
  </div>;
}
