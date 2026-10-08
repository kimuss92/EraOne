import { memo, useEffect, useState } from 'react';
import { Check, ShieldQuestion, X } from 'lucide-react';
import { useHud, useNow } from '../lib/store';
import PathText from './PathText';
import { actions } from '../lib/transport';

// ACTION REQUIRED — Era's single pending confirmation (app/hud/bus_approval.py),
// shown for exactly as long as the backend really holds it.
//
// The buttons are typing shortcuts, not a new approval path: they send the
// bare words "yes" / "no" through the same typed-command route as the command
// bar, so the backend's own gate (_user_approved_pending: the user's latest
// utterance must be a whole "yes", said after the action was armed) decides,
// exactly as if the user had typed it. Nothing is auto-focused, so a stray
// Enter can never approve anything.
//
// The card is never parked on screen: it hides itself after HIDE_AFTER_MS
// (paused while hovered/focused) or on its X. Hiding is display-only — the
// backend still holds the confirmation, so "yes" / "no" keep working until it
// expires there. A NEW confirmation (new id) shows again.

const RISK_CHIP: Record<string, string> = { HIGH: 'chip-bad', MEDIUM: 'chip-warn' };
const RESEND_AFTER_MS = 8000;
export const HIDE_AFTER_MS = 10000;

function expiresIn(sec: number): string {
  if (sec <= 0) return 'expiring';
  return sec >= 60 ? `expires in ${Math.ceil(sec / 60)} min` : `expires in ${Math.ceil(sec)} s`;
}

function ApprovalPrompt() {
  const a = useHud(s => s.approval);
  const online = useHud(s => s.link === 'online');
  const now = useNow();
  const [sent, setSent] = useState<'' | 'yes' | 'no'>('');
  const [hiddenId, setHiddenId] = useState('');
  const [held, setHeld] = useState(false);

  useEffect(() => { setSent(''); setHeld(false); }, [a?.id]);
  const id = a?.id ?? '';
  // Keyed on the id, not the object: state pushes must not restart the clock.
  useEffect(() => {
    if (!id || held || hiddenId === id) return;
    const t = window.setTimeout(() => setHiddenId(id), HIDE_AFTER_MS);
    return () => window.clearTimeout(t);
  }, [id, held, hiddenId]);
  // If Era is still asking a while after an answer was sent (the model did not
  // act on it), let the user answer again rather than leaving dead buttons.
  useEffect(() => {
    if (!sent) return;
    const t = window.setTimeout(() => setSent(''), RESEND_AFTER_MS);
    return () => window.clearTimeout(t);
  }, [sent]);

  if (!a || !online || hiddenId === a.id) return null;
  const answer = (v: 'yes' | 'no') => { if (actions.sendCommand(v)) setSent(v); };

  return (
    <section
      className="approval w-full"
      aria-label="Action required"
      aria-live="assertive"
      tabIndex={-1}
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setHeld(false); }}
      onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); setHiddenId(a.id); } }}
    >
      <div className="flex items-center gap-2">
        <ShieldQuestion className="h-4 w-4 shrink-0 text-[var(--warn)]" aria-hidden="true" />
        <span className="micro text-[var(--warn)]">ACTION REQUIRED</span>
        {a.risk && <span className={`chip ${RISK_CHIP[a.risk] || 'chip-dim'}`}>{a.risk} RISK</span>}
        <span className="ml-auto font-mono text-micro t3">{expiresIn(a.expiresAt - now)}</span>
        <button
          type="button"
          className="icon-btn icon-btn-sm"
          aria-label="Hide this prompt"
          title="Hide (Esc) — you can still say “yes” / “no”"
          onClick={() => setHiddenId(a.id)}
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <p className="mt-1.5 text-body t1">
        ERA ONE wants to <span className="font-medium"><PathText text={a.title} /></span>
        <span className="t3"> · {a.label}</span>
      </p>
      {a.reason && <p className="mt-0.5 select-text text-small t2"><PathText text={a.reason} /></p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" className="mini-btn mini-btn-approve" disabled={!!sent} onClick={() => answer('yes')}>
          <Check className="h-3.5 w-3.5" /> Approve
        </button>
        <button type="button" className="mini-btn" disabled={!!sent} onClick={() => answer('no')}>
          <X className="h-3.5 w-3.5" /> Cancel
        </button>
        <span className="text-meta t3" role="status">
          {sent ? `Sent “${sent}” — waiting for ERA ONE…` : 'or say “yes” / “no”'}
        </span>
      </div>
    </section>
  );
}

export default memo(ApprovalPrompt);
