import { memo, useEffect, useState } from 'react';
import { Check, ShieldQuestion, X } from 'lucide-react';
import { useHud, useNow } from '../lib/store';
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

const RISK_CHIP: Record<string, string> = { HIGH: 'chip-bad', MEDIUM: 'chip-warn' };
const RESEND_AFTER_MS = 8000;

function expiresIn(sec: number): string {
  if (sec <= 0) return 'expiring';
  return sec >= 60 ? `expires in ${Math.ceil(sec / 60)} min` : `expires in ${Math.ceil(sec)} s`;
}

function ApprovalPrompt() {
  const a = useHud(s => s.approval);
  const online = useHud(s => s.link === 'online');
  const now = useNow();
  const [sent, setSent] = useState<'' | 'yes' | 'no'>('');

  useEffect(() => { setSent(''); }, [a?.id]);
  // If Era is still asking a while after an answer was sent (the model did not
  // act on it), let the user answer again rather than leaving dead buttons.
  useEffect(() => {
    if (!sent) return;
    const t = window.setTimeout(() => setSent(''), RESEND_AFTER_MS);
    return () => window.clearTimeout(t);
  }, [sent]);

  if (!a || !online) return null;
  const answer = (v: 'yes' | 'no') => { if (actions.sendCommand(v)) setSent(v); };

  return (
    <section className="approval mx-auto w-full max-w-3xl" aria-label="Action required" aria-live="assertive" tabIndex={-1}>
      <div className="flex items-center gap-2">
        <ShieldQuestion className="h-4 w-4 shrink-0 text-[var(--warn)]" aria-hidden="true" />
        <span className="micro text-[var(--warn)]">ACTION REQUIRED</span>
        {a.risk && <span className={`chip ${RISK_CHIP[a.risk] || 'chip-dim'}`}>{a.risk} RISK</span>}
        <span className="ml-auto font-mono text-micro t3">{expiresIn(a.expiresAt - now)}</span>
      </div>
      <p className="mt-1.5 text-body t1">
        ERA ONE wants to <span className="font-medium">{a.title}</span>
        <span className="t3"> · {a.label}</span>
      </p>
      {a.reason && <p className="mt-0.5 select-text text-small t2">{a.reason}</p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" className="mini-btn mini-btn-approve" disabled={!!sent} onClick={() => answer('yes')}>
          <Check className="h-3.5 w-3.5" /> Approve
        </button>
        <button type="button" className="mini-btn" disabled={!!sent} onClick={() => answer('no')}>
          <X className="h-3.5 w-3.5" /> Reject
        </button>
        <span className="text-meta t3" role="status">
          {sent ? `Sent “${sent}” — waiting for ERA ONE…` : 'or say “yes” / “no”'}
        </span>
      </div>
    </section>
  );
}

export default memo(ApprovalPrompt);
