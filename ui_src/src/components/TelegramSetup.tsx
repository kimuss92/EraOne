import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../lib/transport';

type Status = { step: string; configured?: boolean; locked?: boolean; hasDraft?: boolean };

export default function TelegramSetup({ close }: { close: () => void }) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const root = document.getElementById('root');
    const wasInert = root?.inert ?? false;
    if (root) root.inert = true;
    dialog.current?.focus();
    return () => { if (root) root.inert = wasInert; previous?.focus(); };
  }, []);
  const [status, setStatus] = useState<Status>({ step: 'loading' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [restarting, setRestarting] = useState(false);
  async function submit(action: string, fields: Record<string, unknown> = {}) {
    setBusy(true); setError('');
    try { setStatus(await api<Status>('/api/telegram/setup', { method: 'POST', body: { action, ...fields } })); }
    catch (e) { setError(e instanceof Error ? e.message : 'Setup could not complete.'); }
    finally { setBusy(false); }
  }
  useEffect(() => { void submit('status'); }, []);
  const step = status.step;
  const inputClass = 'w-full rounded border border-[var(--line)] bg-transparent p-2 text-body';
  function field(label: string, name: string, type = 'text', hint = '') {
    return <label className="block space-y-1 text-meta"><span>{label}</span>
      <input className={inputClass} name={name} type={type} required autoComplete="off" placeholder={hint} maxLength={256} />
    </label>;
  }
  return createPortal(<div ref={dialog} tabIndex={-1} onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape' && !busy) close(); }} className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-label="Telegram calling setup">
    <section className="panel w-full max-w-lg max-h-[90vh] overflow-auto p-5 space-y-4">
      <div className="flex justify-between items-center"><h2>Telegram calling setup</h2><button className="chip" onClick={close} disabled={busy}>Close</button></div>
      <p className="t2 text-meta">Your progress is saved on this PC. Enter login details here, never in Era’s conversation.</p>
      {status.locked && <p className="text-meta">Windows cannot unlock the old login. Complete setup to replace it; a backup is kept.</p>}
      {status.configured && step === 'details' && <p className="text-meta">An account is already connected. Only continue if you need to replace or repair its sign-in.</p>}
      {step === 'loading' && <button className="chip" disabled={busy} onClick={() => void submit('status')}>{busy ? 'Checking setup…' : 'Retry setup check'}</button>}
      <form key={step} className="space-y-3" onSubmit={e => {
        e.preventDefault();
        const fields = Object.fromEntries(new FormData(e.currentTarget).entries());
        void submit(step === 'details' ? 'start' : step, fields);
        // Remove short-lived codes and passwords from the input immediately.
        if (step === 'code' || step === 'password') e.currentTarget.reset();
      }}>
        {step === 'details' && <>
          <p className="text-meta">1. Use two different Telegram accounts: Era’s account and your personal account.</p>
          {field('Era’s phone number', 'era_phone', 'tel', '+country code and number')}
          {field('Your personal phone number', 'owner_phone', 'tel', '+country code and number')}
          <p className="text-meta">2. Open <a className="underline" href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">Telegram Development Tools</a>, sign in, then copy App api_id and App api_hash. If asked to create an app, use a title such as Era One and choose Desktop.</p>
          {field('Application API ID', 'api_id', 'number')}
          {field('Application API hash', 'api_hash', 'password')}
        </>}
        {step === 'code' && <><p className="text-meta">Open Era’s Telegram account and find the login code sent by Telegram.</p>{field('Telegram login code', 'code', 'password')}</>}
        {step === 'password' && <><p className="text-meta">This account has Telegram two-step verification enabled.</p>{field('Telegram two-step password', 'password', 'password')}</>}
        {step === 'owner' && <><p className="text-meta">Era is signed in. Enter your personal Telegram username. We check it against your saved owner ID. If no owner ID is configured, your phone must be visible to Era for verification.</p>{field('Your personal @username', 'username', 'text', '@yourusername')}</>}
        {step === 'enable' && <p className="text-meta">Era’s login and your identity are verified. Enable owner-only calling and incoming calls, then restart Era to load this connection.</p>}
        {!['done', 'loading'].includes(step) && <button className="chip" type="submit" disabled={busy}>{busy ? 'Working…' : step === 'enable' ? 'Enable calls' : step === 'details' ? 'Send login code' : 'Continue'}</button>}
      </form>
      {step === 'done' && <div className="space-y-3"><p className="text-meta">Calls are enabled. Restart Era, then call her from your personal Telegram account to check the sound.</p>
        <button className="chip" disabled={restarting} onClick={async () => {
          setRestarting(true); setError('');
          try { const result = await api<{ accepted: boolean }>('/api/restart', { method: 'POST' }); if (!result.accepted) throw new Error('Era could not accept the restart. Try the Restart control again.'); }
          catch (e) { setError(e instanceof Error ? e.message : 'Restart failed.'); setRestarting(false); }
        }}>{restarting ? 'Restart requested…' : 'Restart Era now'}</button></div>}
      {error && <p role="alert" className="text-meta text-[var(--warn)]">{error}</p>}
      {step !== 'done' && <button className="chip" disabled={busy} onClick={() => void submit('cancel')}>Discard setup progress</button>}
    </section>
  </div>, document.body);
}
