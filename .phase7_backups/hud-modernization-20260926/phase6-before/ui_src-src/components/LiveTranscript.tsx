import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, History, Maximize2, Minimize2 } from 'lucide-react';
import { setState, useHud } from '../lib/store';
import type { ConversationEntry } from '../lib/types';

// SESSION TRANSCRIPT — the conversation as part of the HUD, not a chat widget.
//
// History is the backend's own conversation record (seeded by the snapshot,
// so a reload keeps it); while a turn is in flight, a LIVE row shows the
// user's words as they are transcribed and Era's reply as it streams. A live
// row gives way to the recorded entry for it the moment that entry arrives
// (matched by start time, so a turn is never shown twice).
//
// Scrolling respects the reader: it follows new content only while the view
// is at the bottom; scrolled up, a "New activity" pill appears instead of
// yanking the view. The region has a FIXED height (compact or expanded), so
// arriving messages never resize the Neuracore stage. System/log lines stay
// in the conversation drawer.

const EXPANDED_KEY = 'era_hud_transcript_expanded';
const MAX_ROWS = 80;
const RECENT_ROWS = 4; // older rows recede to secondary text
const AT_BOTTOM_PX = 24;

interface Row {
  id: string;
  role: 'user' | 'agent';
  text: string;
  live: boolean;
  ts: number;
  source?: string;
}

function loadExpanded(): boolean {
  try { return localStorage.getItem(EXPANDED_KEY) === '1'; } catch { return false; }
}

function LiveTranscript() {
  const entries = useHud(s => s.conversation);
  const t = useHud(s => s.transcript);
  const state = useHud(s => s.agent.state);
  const [expanded, setExpanded] = useState(loadExpanded);
  const [unseen, setUnseen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);

  const rows = useMemo<Row[]>(() => {
    const talk = entries.filter((e): e is ConversationEntry & { role: 'user' | 'agent' } => e.role !== 'system');
    const out: Row[] = talk.slice(-MAX_ROWS).map(e => ({ id: e.id, role: e.role, text: e.text, live: false, ts: e.ts, source: e.source }));
    // Both timestamps come from the backend clock, so the match is strict: only
    // an entry recorded at/after the live text began can be its record.
    const recordedAfter = (role: 'user' | 'agent', at?: number) =>
      talk.some(e => e.role === role && e.ts >= (at ?? 0));
    if (t.user && !t.userFinal && !recordedAfter('user', t.userAt)) {
      out.push({ id: 'live-user', role: 'user', text: t.user, live: true, ts: t.userAt ?? 0 });
    }
    if (t.response && !recordedAfter('agent', t.respAt)) {
      out.push({ id: 'live-agent', role: 'agent', text: t.response, live: true, ts: t.respAt ?? 0 });
    }
    return out;
  }, [entries, t]);

  // History (anything recorded before this view opened — it arrives in the
  // snapshot, after first paint) is not news: only entries recorded later get
  // the one-shot highlight (a CSS animation runs once, when a row mounts).
  // Live rows never flash — the caret already says they are live.
  const [openedAt] = useState(() => Date.now() / 1000);
  const fresh = (r: Row) => !r.live && r.ts > openedAt;

  useEffect(() => {
    try { localStorage.setItem(EXPANDED_KEY, expanded ? '1' : '0'); } catch { /* private mode */ }
  }, [expanded]);

  // Follow new content only when the reader is already at the bottom.
  const last = rows[rows.length - 1];
  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    if (atBottom.current) el.scrollTop = el.scrollHeight;
    else setUnseen(true);
  }, [rows.length, last?.text]);

  // Expanding/collapsing keeps the reader's place at the bottom.
  useLayoutEffect(() => {
    const el = boxRef.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [expanded]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_PX;
    if (atBottom.current) setUnseen(false);
  };

  const jump = () => {
    const el = boxRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    atBottom.current = true;
    setUnseen(false);
  };

  const transcribing = state === 'transcribing';
  return (
    <div className={`transcript relative mx-auto w-full max-w-3xl ${expanded ? 'transcript-expanded' : ''}`}>
      <div className="transcript-tools">
        <button type="button" className="icon-btn icon-btn-sm" title="Full conversation history" aria-label="Full conversation history" onClick={() => setState({ drawer: 'conversation' })}>
          <History className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          className="icon-btn icon-btn-sm"
          title={expanded ? 'Collapse transcript' : 'Expand transcript'}
          aria-label={expanded ? 'Collapse transcript' : 'Expand transcript'}
          aria-pressed={expanded}
          onClick={() => setExpanded(x => !x)}
        >
          {expanded ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
        </button>
      </div>
      <div
        ref={boxRef}
        onScroll={onScroll}
        className="transcript-scroll custom-scrollbar"
        role="log"
        aria-label="Conversation"
        tabIndex={0}
      >
        {rows.length === 0 && (
          <p className="px-2 pt-2 text-small t3">
            The conversation will appear here. Say the wake word, or type below.
          </p>
        )}
        {rows.map((r, i) => {
          const recent = i >= rows.length - RECENT_ROWS;
          return (
            <div key={r.id} className={`tr-row ${fresh(r) ? 'tr-new' : ''}`}>
              <span className={`micro tr-who ${r.role === 'agent' ? 'ac-text' : ''}`}>{r.role === 'agent' ? 'ERA ONE' : 'YOU'}</span>
              <p className={`tr-text select-text ${recent || r.live ? 't1' : 't2'} ${r.live && r.role === 'user' ? 'tr-partial' : ''}`}>
                {r.text}
                {r.live && (r.role === 'agent' ? t.responding : transcribing) && <span className="caret" />}
              </p>
            </div>
          );
        })}
      </div>
      {unseen && (
        <button type="button" className="tr-unseen" onClick={jump}>
          <ArrowDown className="h-3 w-3" /> New activity
        </button>
      )}
    </div>
  );
}

export default memo(LiveTranscript);
