import { memo, useEffect, useRef } from 'react';
import { useHud } from '../lib/store';

// Dialogue as it happens, not a chat log: the user's words while they are
// still being transcribed (Gemini's partial input transcription), then Era's
// reply streaming in as the model produces it. Full history lives in the
// conversation drawer.

function LiveTranscript() {
  const t = useHud(s => s.transcript);
  const state = useHud(s => s.agent.state);
  const respRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    const el = respRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [t.response]);

  if (!t.user && !t.response) return null;
  const transcribing = state === 'transcribing' && !t.userFinal;
  const current = t.responding || state === 'speaking' || state === 'synthesizing';
  return (
    <div className="transcript mx-auto w-full max-w-3xl space-y-2 px-2" aria-live="polite">
      {t.user && (
        <div className="flex gap-3">
          <span className="micro w-14 shrink-0 pt-0.5 text-right">YOU</span>
          <p className={`min-w-0 flex-1 select-text text-body leading-snug ${t.userFinal ? 't1' : 't2'}`}>
            {t.user}
            {transcribing && <span className="caret" />}
          </p>
        </div>
      )}
      {t.response && (
        <div className="flex gap-3">
          <span className="micro w-14 shrink-0 pt-0.5 text-right ac-text">ERA ONE</span>
          <p ref={respRef} className={`transcript-resp min-w-0 flex-1 select-text text-body leading-snug ${current ? 't1' : 't2'}`}>
            {t.response}
            {t.responding && <span className="caret" />}
          </p>
        </div>
      )}
    </div>
  );
}

export default memo(LiveTranscript);
