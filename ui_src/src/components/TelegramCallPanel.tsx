import { useState } from 'react';
import { Phone, PhoneOff } from 'lucide-react';
import { useHud } from '../lib/store';
import { send } from '../lib/transport';
import TelegramSetup from './TelegramSetup';

const LABELS: Record<string, string> = {
  disabled: 'Calls not enabled', idle: 'Ready', starting: 'Starting', waiting: 'Waiting for your call',
  incoming: 'Incoming call', connecting: 'Connecting', connected: 'Connected', ending: 'Ending call',
  ended: 'Call ended', stopped: 'Session stopped', failed: 'Call failed',
  recovering: 'Retrying call cleanup',
};
const REASONS: Record<string, string> = {
  credentials_unreadable: 'Windows cannot unlock Era’s saved Telegram sign-in. Choose Set up / Repair Telegram to sign in again. Your old login will be backed up.',
  account_not_configured: 'Choose Set up / Repair Telegram to connect Era’s account.',
  validation_pending: 'Call testing is disabled or its 30-minute window has expired. Enable a new window below.',
  runtime_policy_pending: 'Call integration is still being completed.',
  cleanup_incomplete: 'Call cleanup needs attention before another call can start.',
  remote_hangup_unconfirmed: 'Telegram has not confirmed remote hangup.',
};
const ERRORS: Record<string, string> = {
  call_privacy: 'Your Telegram call privacy settings block Era. On your personal account, open Settings → Privacy and Security → Calls → Always Allow and add Era’s account.',
  no_answer: 'The call was not answered in time.',
  calls_disabled: 'The attended call test window is not enabled.',
  test_window_expired: 'The attended call test window has expired.',
  credentials_unreadable: 'Saved Telegram credentials could not be opened.',
  auth_required: 'Era’s Telegram account needs authentication again.',
  identity_invalid: 'The Telegram account binding needs verification.',
  native_failed: 'The native Telegram connection failed. Remote audio was not verified.',
  provider_rate_limited: 'Gemini’s call quota is temporarily exhausted. Retry later or configure a backup Gemini key.',
  provider_unavailable: 'Gemini is temporarily unavailable. Please retry later.',
  provider_failed: 'Gemini’s audio session failed. Call audio was interrupted.',
  protocol_failed: 'That call’s connection closed unexpectedly. You can start another call once cleanup finishes.',
  cleanup_incomplete: 'Call cleanup needs attention. Wait for the call controls to become available before retrying.',
};

export default function TelegramCallPanel() {
  const call = useHud(s => s.telegramCall);
  const online = useHud(s => s.link === 'online');
  const [error, setError] = useState('');
  const [setupOpen, setSetupOpen] = useState(false);
  if (!call) return null;
  function control(action: string) {
    setError(send({ type: 'telegram_call', action, generation: call?.generation }) ? '' : 'The HUD is disconnected.');
  }
  return (
    <section className="panel shrink-0" aria-label="Telegram calls">
      <div className="panel-head"><span className="micro">Telegram calls</span><Phone className="h-3.5 w-3.5 t2" /></div>
      <div className="space-y-2 p-3">
        <button className="chip" disabled={!online} onClick={() => setSetupOpen(true)}>Set up / Repair Telegram</button>
        {setupOpen && <TelegramSetup close={() => setSetupOpen(false)} />}
        <p className="text-body" role="status">{LABELS[call.state] || 'Status unavailable'}</p>
        {call.reason && <p className="t2 text-meta">{REASONS[call.reason] || 'Calls are unavailable.'}</p>}
        {call.error && <p role="alert" className="text-meta text-[var(--warn)]">{ERRORS[call.error] || 'The call could not finish normally. Check diagnostics before retrying.'}</p>}
        {call.receivingRetrying && <p className="t2 text-meta">Incoming calls will reconnect automatically.</p>}
        <div className="flex flex-wrap gap-2">
          {call.canRenewTest && <button type="button" className="chip" disabled={!online} onClick={() => control('renew_test')}>Enable calls for 30 minutes</button>}
          <button type="button" className="chip" disabled={!online || !(call.canDial ?? call.canStart)} onClick={() => control('dial')}>Call owner</button>
          {call.listeningArmed
            ? <button type="button" className="chip" disabled={!online} onClick={() => control('stop_listening')}>Stop receiving</button>
            : <button type="button" className="chip" disabled={!online || !call.canStart} onClick={() => control('listen')}>Receive calls</button>}
          {call.listeningArmed && call.canStart && <button type="button" className="chip" disabled={!online} onClick={() => control('listen')}>Resume receiving</button>}
          {call.canHangup && <button type="button" className="chip" disabled={!online} onClick={() => control('hangup')}><PhoneOff className="mr-1 inline h-3 w-3" />Hang up</button>}
          {call.canRecover && <button type="button" className="chip" disabled={!online} onClick={() => control('recover')}>Retry cleanup</button>}
        </div>
        {(call.inputCaption || call.outputCaption) && <div className="space-y-1 text-meta select-text">
          {call.inputCaption && <p>You: {call.inputCaption}</p>}
          {call.outputCaption && <p className="text-[var(--accent)]">Era: {call.outputCaption}</p>}
        </div>}
        {error && <p role="alert" className="text-meta text-[var(--warn)]">{error}</p>}
      </div>
    </section>
  );
}
