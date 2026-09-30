import { forwardRef, memo, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Mic, MicOff, Paperclip, Power, Send, Square } from 'lucide-react';
import { getState, live, useHud } from '../lib/store';
import { actions } from '../lib/transport';

const HISTORY_KEY = 'era_cmd_history';
const HISTORY_MAX = 50;

function loadHistory(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    return Array.isArray(v) ? v.filter(x => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

// Scrolling level trace: the REAL mic RMS while the user can be heard, Era's
// REAL voice amplitude while she speaks. Canvas + rAF, never React state.
function VoiceWave() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current!;
    const g = c.getContext('2d')!;
    const hist = new Float32Array(56);
    let raf = 0, last = 0, head = 0, colorAt = 0, micColor = '#5fd4ff', voiceColor = '#5fe0c8';
    const draw = (now: number) => {
      raf = requestAnimationFrame(draw);
      if (document.hidden || now - last < 50) return;
      last = now;
      const s = getState();
      const speaking = s.agent.state === 'speaking';
      const v = speaking ? live.amplitude : s.micActive && !s.standby ? Math.min(1, Math.sqrt(live.micLevel) * 2.2) : 0;
      hist[head] = v;
      head = (head + 1) % hist.length;
      const w = c.clientWidth, h = c.clientHeight, dpr = Math.min(2, window.devicePixelRatio || 1);
      if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const n = hist.length, bw = w / n;
      if (now - colorAt > 1000) {
        // Theme colours, re-read once a second rather than on every frame.
        const css = getComputedStyle(c);
        micColor = css.getPropertyValue('--accent').trim() || micColor;
        voiceColor = css.getPropertyValue('--accent-2').trim() || voiceColor;
        colorAt = now;
      }
      g.fillStyle = speaking ? voiceColor : micColor;
      for (let i = 0; i < n; i++) {
        const val = hist[(head + i) % n];
        const bh = Math.max(1.5, val * (h - 4));
        g.globalAlpha = 0.25 + 0.75 * (i / n);
        g.fillRect(i * bw + bw * 0.2, (h - bh) / 2, bw * 0.6, bh);
      }
      g.globalAlpha = 1;
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);
  return <canvas ref={ref} className="h-9 w-28 shrink-0 xl:w-36" aria-hidden="true" />;
}

export interface CommandBarHandle {
  focus: () => void;
  setText: (t: string) => void;
}

interface Props {
  onFiles: (files: File[]) => void;
}

const CommandBar = forwardRef<CommandBarHandle, Props>(function CommandBar({ onFiles }, ref) {
  const link = useHud(s => s.link);
  const micActive = useHud(s => s.micActive);
  const standby = useHud(s => s.standby);
  const state = useHud(s => s.agent.state);
  const [text, setText] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const hist = useRef<string[]>(loadHistory());
  const pos = useRef(-1);
  const draft = useRef('');

  useImperativeHandle(ref, () => ({
    focus: () => inputRef.current?.focus(),
    setText: (t: string) => { setText(t); inputRef.current?.focus(); },
  }));

  const online = link === 'online';
  const speaking = state === 'speaking';
  const micState = !online ? 'MIC DISCONNECTED' : standby ? 'STANDBY · SAY THE WAKE WORD' : micActive ? 'MIC ACTIVE' : 'MIC OFF';

  const submit = () => {
    const msg = text.trim();
    if (!msg || !online) return;
    if (!actions.sendCommand(msg)) return;
    setText('');
    const h = hist.current;
    if (h[h.length - 1] !== msg) {
      h.push(msg);
      if (h.length > HISTORY_MAX) h.splice(0, h.length - HISTORY_MAX);
      try { localStorage.setItem(HISTORY_KEY, JSON.stringify(h)); } catch { /* private mode */ }
    }
    pos.current = -1;
  };

  const recall = (dir: -1 | 1) => {
    const h = hist.current;
    if (!h.length) return;
    let p = pos.current;
    if (dir === -1) {
      if (p === -1) { draft.current = text; p = h.length - 1; } else if (p > 0) p -= 1; else return;
    } else {
      if (p === -1) return;
      p = p < h.length - 1 ? p + 1 : -1;
    }
    pos.current = p;
    setText(p === -1 ? draft.current : h[p]);
  };

  return (
    <div className="mx-auto w-full max-w-3xl">
      <form
        className="cmd-bar flex items-end gap-2 p-2"
        onSubmit={e => { e.preventDefault(); submit(); }}
      >
        {standby ? (
          <button type="button" className="round-btn round-btn-warn" title="Wake ERA ONE" disabled={!online} onClick={() => actions.setStandby(false)}>
            <Power className="h-4.5 w-4.5" />
          </button>
        ) : (
          <button
            type="button"
            className={`round-btn ${micActive ? 'round-btn-on' : 'round-btn-off'}`}
            title={micActive ? 'Turn the microphone off' : 'Turn the microphone on'}
            aria-pressed={micActive}
            disabled={!online}
            onClick={() => actions.setMic(!micActive)}
          >
            {micActive ? <Mic className="h-4.5 w-4.5" /> : <MicOff className="h-4.5 w-4.5" />}
          </button>
        )}
        <div className="hidden sm:block"><VoiceWave /></div>
        <textarea
          ref={inputRef}
          rows={1}
          value={text}
          onChange={e => { setText(e.target.value); pos.current = -1; }}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
            else if (e.key === 'ArrowUp' && (text === '' || pos.current !== -1)) { e.preventDefault(); recall(-1); }
            else if (e.key === 'ArrowDown' && pos.current !== -1) { e.preventDefault(); recall(1); }
          }}
          placeholder={online ? 'Ask ERA ONE anything…' : 'Waiting for ERA ONE…'}
          title="Enter to send · Shift+Enter new line · ↑/↓ history · / or Ctrl+K"
          disabled={!online}
          className="cmd-input min-h-[40px] min-w-0 flex-1 resize-none px-3 py-2.5 text-[13.5px]"
        />
        <input ref={fileRef} type="file" multiple hidden onChange={e => { onFiles(Array.from(e.target.files || [])); e.currentTarget.value = ''; }} />
        <button type="button" className="round-btn" title="Attach files" disabled={!online} onClick={() => fileRef.current?.click()}>
          <Paperclip className="h-4 w-4" />
        </button>
        <button
          type="button"
          className={`round-btn ${speaking ? 'round-btn-warn' : ''}`}
          title="Stop speaking (Esc)"
          disabled={!online || !speaking}
          onClick={() => actions.interrupt()}
        >
          <Square className="h-3.5 w-3.5" />
        </button>
        <button type="submit" className="round-btn round-btn-accent" title="Send" disabled={!online || !text.trim()}>
          <Send className="h-4 w-4" />
        </button>
      </form>
      <div className="mt-1.5 flex justify-center">
        <span className={`micro ${!online || !micActive || standby ? 'text-[var(--warn)]' : ''}`}>{micState}</span>
      </div>
    </div>
  );
});

export default memo(CommandBar);
