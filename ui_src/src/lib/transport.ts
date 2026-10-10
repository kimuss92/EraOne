import { EMPTY, getState, live, setState } from './store';
import type {
  AgentEvent,
  AgentState,
  Approval,
  ContextCard,
  ConversationEntry,
  ModelChoice,
  Source,
  TimelineItem,
  ToolRun,
} from './types';
import { cue } from './sounds';
import { STATE_HUE } from './format';
import { noticeFromEvent, resolve } from './notifications';

// ── Where the backend is, and how to prove we may talk to it ────────────
// Served by the backend (http://127.0.0.1:<port>/): same origin, and the
// session token is injected into the page as a <meta>. Loaded by the legacy Qt
// window (file://): the window passes host/token/port in the query string.

const query = new URLSearchParams(window.location.search);
export const isQtHost = query.get('host') === 'qt';
const served = window.location.protocol.startsWith('http');
const port = query.get('port') || '8765';
const token =
  document.querySelector<HTMLMetaElement>('meta[name="eraone-token"]')?.content ||
  query.get('token') ||
  '';
const origin = served ? '' : `http://127.0.0.1:${port}`;
const wsUrl = (served ? `ws://${window.location.host}` : `ws://127.0.0.1:${port}`) + `/ws?token=${encodeURIComponent(token)}`;

const MAX_CONVERSATION = 400;
const MAX_TOOLS = 60;
const MAX_CARDS = 40;
const MAX_SOURCES = 40;
const MAX_TIMELINE = 40;
const MAX_RESPONSE = 4000;

let ws: WebSocket | null = null;
let retry = 0;
let reconnectTimer = 0;
let pingTimer = 0;
let lastStateSeq = 0;
let failedHandshakes = 0;

// ── REST ─────────────────────────────────────────────────────────────────

export async function api<T = unknown>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const res = await fetch(origin + path, {
    method: init?.method ?? 'GET',
    headers: {
      'X-EraOne-Token': token,
      ...(init?.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    cache: 'no-store',
  });
  const text = await res.text();
  let data: Record<string, unknown> = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    // Error pages (e.g. aiohttp's plain-text 404) are not JSON; only a 2xx must be.
    if (res.ok) throw new Error('The backend sent an unreadable response');
  }
  if (!res.ok) {
    throw Object.assign(new Error((data.error as string) || `HTTP ${res.status}`), { status: res.status });
  }
  return data as T;
}

// ── WebSocket ────────────────────────────────────────────────────────────

export function send(msg: Record<string, unknown>): boolean {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
    return true;
  }
  return false;
}

export function connect(): void {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  let opened = false;
  try {
    ws = new WebSocket(wsUrl);
  } catch {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    opened = true;
    failedHandshakes = 0;
    retry = 0;
    setState({ link: 'online' });
    window.clearInterval(pingTimer);
    const ping = () => send({ type: 'ping', t: performance.now() });
    ping();
    pingTimer = window.setInterval(ping, 5000);
  };
  ws.onclose = () => {
    if (!opened) failedHandshakes += 1;
    window.clearInterval(pingTimer);
    live.amplitude = 0;
    live.bands.fill(0);
    live.micLevel = 0;
    ws = null;
    // Nothing live is known while the link is down: drop live readings to
    // unknown rather than keep showing the last ones as if they were current.
    // (History — tool runs, conversation, sources — is a record and stays.)
    setState({
      link: 'reconnecting',
      rttMs: null,
      metrics: EMPTY.metrics,
      connection: { kind: 'unknown', label: 'Unknown', status: '' },
      voiceActive: false,
    });
    scheduleReconnect();
  };
  ws.onerror = () => ws?.close();
  ws.onmessage = ev => {
    try {
      handle(JSON.parse(ev.data));
    } catch {
      /* a malformed frame costs that frame, never the session */
    }
  };
}

function scheduleReconnect(): void {
  window.clearTimeout(reconnectTimer);
  const delay = Math.min(5000, 800 + retry * 700);
  retry += 1;
  reconnectTimer = window.setTimeout(() => void reconnect(), delay);
}

// A restarted backend (crash self-heal, "reboot Era One") issues a NEW session
// token, so this page's token is refused and the socket can never open again.
// When handshakes keep failing while the tokenless health check says the
// backend is up, reload: the fresh page carries the fresh token, and history is
// re-seeded from the backend's snapshot on connect.
async function reconnect(): Promise<void> {
  if (served && failedHandshakes >= 2) {
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      const h = await r.json();
      if (r.ok && h && h.app === 'eraone') {
        window.location.reload();
        return;
      }
    } catch {
      /* backend still down: keep retrying below */
    }
  }
  connect();
}

// Browsers throttle timers in hidden windows; come back at once when visible.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !ws) {
    window.clearTimeout(reconnectTimer);
    void reconnect();
  }
});

// ── Reducer ──────────────────────────────────────────────────────────────

function upsertTool(run: ToolRun): void {
  setState(s => {
    const tools = s.tools.filter(t => t.id !== run.id);
    tools.push(run);
    return { tools: tools.slice(-MAX_TOOLS) };
  });
}

function pushTimeline(item: Omit<TimelineItem, 'id'>): void {
  setState(s => ({
    timeline: [...s.timeline, { ...item, id: `${item.ts}-${Math.random().toString(36).slice(2, 7)}` }].slice(
      -MAX_TIMELINE,
    ),
  }));
}

function upsertTaskTimeline(item: TimelineItem): void {
  setState(s => ({
    timeline: [...s.timeline.filter(existing => existing.id !== item.id), item]
      .sort((a, b) => a.ts - b.ts)
      .slice(-MAX_TIMELINE),
  }));
}

function mapModel(data: Record<string, unknown>) {
  return {
    label: String(data.label || ''),
    alias: data.alias ? String(data.alias) : undefined,
    detail: data.detail ? String(data.detail) : undefined,
    fellBackFrom: data.fell_back_from ? String(data.fell_back_from) : undefined,
    fallbackReason: data.fallback_reason ? String(data.fallback_reason) : undefined,
    pinned: Boolean(data.pinned),
  };
}

function mapCatalog(rows: unknown): ModelChoice[] {
  return (Array.isArray(rows) ? rows : [])
    .map((m: Record<string, unknown>) => ({
      alias: String(m.alias || ''),
      label: String(m.label || ''),
      detail: String(m.detail || ''),
      role: String(m.role || ''),
      available: m.available !== false,
      unavailableReason: String(m.unavailable_reason || ''),
      costPerCallUsd: Number(m.cost_per_call_usd) || 0,
      realtimeAudio: Boolean(m.realtime_audio),
    }))
    .filter(m => m.alias);
}

function applySnapshot(d: Record<string, any>): void {
  lastStateSeq = 0;
  setState({
    agent: { state: (typeof d.state === "string" && d.state in STATE_HUE ? d.state : "idle") as AgentState, label: String(d.label || ''), seq: 0 },
    connection: d.connection && typeof d.connection === "object" ? d.connection : { kind: "unknown", label: "Unknown", status: "" },
    standby: Boolean(d.standby),
    micActive: Boolean(d.micActive),
    voiceActive: Boolean(d.voiceActive),
    transcript: {
      user: typeof d.transcript?.user === 'string' ? d.transcript.user : '',
      response: typeof d.transcript?.response === 'string' ? d.transcript.response : '',
      userFinal: Boolean(d.transcript?.userFinal), responding: Boolean(d.transcript?.responding),
      userAt: Number(d.transcript?.userAt) || undefined, respAt: Number(d.transcript?.respAt) || undefined,
    },
    tools: Array.isArray(d.tools) ? d.tools.filter((t: any) => t && typeof t.id === "string" && typeof t.activity === "string" && typeof t.status === "string") : [],
    cards: Array.isArray(d.cards) ? d.cards.filter((c: any) => c && typeof c.id === "string" && typeof c.title === "string" && typeof c.subtitle === "string") : [],
    sources: Array.isArray(d.sources) ? d.sources.filter((s: any) => s && typeof s.id === "string" && typeof s.title === "string") : [],
    conversation: Array.isArray(d.conversation) ? d.conversation.filter((c: any) => c && typeof c.id === "string" && typeof c.text === "string") : [],
    latency: d.latency || null,
    memory: d.memory || getState().memory,
    backendStartedAt: Number(d.startedAt) || null,
    approval: (d.approval as Approval) || null,
    ...(d.model ? { model: mapModel(d.model) } : {}),
    ...(d.catalog ? { catalog: mapCatalog(d.catalog.models), selectedModel: String(d.catalog.selected || '') } : {}),
  });
  // A reload / reconnect learns about a waiting approval from the snapshot, not an event.
  if (d.approval) {
    noticeFromEvent({ id: '', seq: 0, ts: Date.now() / 1000, sessionId: '', type: 'approval.requested', label: d.approval.title, metadata: d.approval });
  } else {
    resolve('approval');
  }
}

function onEvent(e: AgentEvent): void {
  const md = (e.metadata || {}) as Record<string, any>;
  noticeFromEvent(e);
  switch (e.type) {
    case 'visual.changed':
      window.dispatchEvent(new Event('era:visual'));
      break;
    case 'development.changed':
      window.dispatchEvent(new Event('era:development'));
      break;
    case 'schedule.changed':
      window.dispatchEvent(new Event('era:schedule'));
      break;
    case 'task.status.changed': {
      const taskId = String(md.task_id || '');
      const seq = Number(md.seq);
      const status = String(md.status || 'unknown');
      if (!taskId || !Number.isFinite(seq)) break;
      const tone = status === 'completed' ? 'ok' : status === 'failed' ? 'bad' : status === 'cancelled' ? 'warn' : 'info';
      upsertTaskTimeline({
        id: `task-${taskId}-${seq}`,
        ts: e.ts,
        kind: 'task',
        label: `Task ${status} · ${String(md.goal || '').slice(0, 120)}`,
        detail: String(md.error || md.result || ''),
        tone,
      });
      break;
    }
    case 'agent.state.changed':
      if (e.seq > lastStateSeq) {
        lastStateSeq = e.seq;
        setState({ agent: { state: e.state as AgentState, label: e.label || '', seq: e.seq } });
      }
      break;
    case 'connection.status':
      setState({ connection: { kind: e.state || '', label: e.label || '', status: e.message || '' } });
      pushTimeline({
        ts: e.ts,
        kind: 'connection',
        label: `Model link · ${e.label}`,
        detail: e.message,
        tone: e.state === 'connected' || e.state === 'local' ? 'ok' : e.state === 'reconnecting' || e.state === 'connecting' ? 'warn' : 'bad',
      });
      break;
    case 'voice.wake_detected':
      pushTimeline({ ts: e.ts, kind: 'wake', label: 'Wake word detected', tone: 'ok' });
      cue('wake');
      break;
    case 'voice.listening.started':
      setState({ voiceActive: true });
      break;
    case 'voice.listening.stopped':
      setState({ voiceActive: false });
      break;
    case 'voice.transcript.partial':
      setState(s => ({
        transcript: {
          ...s.transcript,
          user: e.message || '',
          userFinal: false,
          // a new utterance starts when the previous one was final (or empty)
          userAt: s.transcript.userFinal || !s.transcript.user ? e.ts : s.transcript.userAt,
        },
      }));
      break;
    case 'voice.transcript.final':
      setState(s => ({ transcript: { ...s.transcript, user: e.message || '', userFinal: true, userAt: s.transcript.userAt ?? e.ts } }));
      break;
    case 'agent.routing.completed':
      pushTimeline({ ts: e.ts, kind: 'routing', label: e.label || 'Routed', duration: e.duration, tone: 'info' });
      break;
    case 'agent.request.sent':
      setState(s => ({ transcript: { ...s.transcript, response: '', responding: false, respAt: undefined } }));
      break;
    case 'agent.response.started':
      setState(s => ({ transcript: { ...s.transcript, response: '', responding: true, respAt: e.ts } }));
      break;
    case 'agent.response.delta':
      setState(s => ({
        transcript: {
          ...s.transcript,
          responding: true,
          respAt: s.transcript.respAt ?? e.ts,
          response: `${s.transcript.response} ${e.message || ''}`.trim().slice(-MAX_RESPONSE),
        },
      }));
      break;
    case 'agent.response.completed':
      setState(s => ({
        transcript: { ...s.transcript, responding: false, response: e.message || s.transcript.response },
      }));
      if (!md.interrupted && e.message) cue('ready');
      break;
    case 'tts.interrupted':
      pushTimeline({ ts: e.ts, kind: 'interrupt', label: 'Interrupted', detail: String(md.source || ''), tone: 'warn' });
      break;
    case 'tool.started':
      upsertTool(md as unknown as ToolRun);
      setState(s => ({ toolPulse: { seq: s.toolPulse.seq + 1, category: String(md.category || '') } }));
      break;
    case 'tool.completed':
    case 'tool.failed':
    case 'tool.cancelled':
    case 'tool.progress':
      upsertTool(md as unknown as ToolRun);
      if (e.type === 'tool.completed' && !md.background) cue('tool');
      if (e.type === 'tool.failed' && md.status === 'failed') cue('error');
      // A task was created / changed / cancelled / run: refresh the schedule view.
      if (e.toolName === 'scheduled_task' && e.type !== 'tool.progress') window.dispatchEvent(new Event('era:schedule'));
      break;
    case 'context.added':
      if (md.card) setState(s => ({ cards: [...s.cards, md.card as ContextCard].slice(-MAX_CARDS) }));
      break;
    case 'source.added':
      if (md.source) setState(s => ({ sources: [...s.sources, md.source as Source].slice(-MAX_SOURCES) }));
      break;
    case 'memory.retrieved':
      setState(s => ({
        memory: {
          retrieved_total: s.memory.retrieved_total + Number(md.count || 0),
          last_count: Number(md.count || 0),
          last_at: e.ts,
        },
      }));
      break;
    case 'conversation.entry':
      if (md.entry) {
        setState(s => ({
          conversation: [...s.conversation, md.entry as ConversationEntry].slice(-MAX_CONVERSATION),
        }));
      }
      break;
    case 'approval.requested':
      setState({ approval: md as unknown as Approval });
      pushTimeline({ ts: e.ts, kind: 'approval', label: `Approval requested · ${e.label || md.tool || ''}`, tone: 'warn' });
      break;
    case 'approval.resolved':
      setState(s => (s.approval && s.approval.id === md.id ? { approval: null } : {}));
      break;
    case 'error':
      pushTimeline({ ts: e.ts, kind: 'error', label: e.message || 'Error', tone: 'bad' });
      cue('error');
      break;
  }
}

function handle(msg: Record<string, any>): void {
  switch (msg.type) {
    case 'event':
      onEvent(msg.event as AgentEvent);
      break;
    case 'snapshot':
      applySnapshot(msg.data || {});
      break;
    case 'amplitude': {
      live.amplitude = Math.max(0, Math.min(1, Number(msg.value) || 0));
      if (Array.isArray(msg.bands)) {
        for (let i = 0; i < live.bands.length; i++) {
          const v = Number(msg.bands[i]);
          live.bands[i] = Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0;
        }
      }
      break;
    }
    case 'voice_level':
      live.micLevel = Math.max(0, Math.min(1, Number(msg.level) || 0));
      if (Boolean(msg.active) !== getState().voiceActive) setState({ voiceActive: Boolean(msg.active) });
      break;
    case 'state':
      if (msg.state !== 'SPEAKING') {
        live.amplitude = 0;
        live.bands.fill(0);
      }
      break;
    case 'mute':
      setState({ standby: Boolean(msg.muted) });
      break;
    case 'browser_mic':
      setState({ micActive: Boolean(msg.active) });
      if (!msg.active) live.micLevel = 0;
      break;
    case 'system_metrics':
      if (msg.data) setState(s => ({ metrics: { ...s.metrics, ...msg.data } }));
      break;
    case 'model':
      setState({ model: mapModel(msg) });
      break;
    case 'model_catalog':
      setState({ catalog: mapCatalog(msg.models), selectedModel: String(msg.selected || '') });
      break;
    case 'theme': {
      const t = String(msg.theme || 'toggle');
      setState(s => ({ theme: t === 'toggle' ? (s.theme === 'dark' ? 'light' : 'dark') : t === 'light' ? 'light' : 'dark' }));
      break;
    }
    case 'pong':
      if (typeof msg.t === 'number') setState({ rttMs: Math.max(0, Math.round(performance.now() - msg.t)) });
      break;
  }
}

// ── Actions (every one is a real backend operation) ─────────────────────

export const actions = {
  sendCommand(text: string): boolean {
    return send({ type: 'command', text });
  },
  interrupt(): void {
    if (!send({ type: 'interrupt' })) void api('/api/interrupt', { method: 'POST' }).catch(() => {});
  },
  /** Cancel one running task by its tool-run id (the X on an Activity row).
   *  Its own message type rather than a command, because a cancel has to
   *  reach the engine exactly when the model's channel is not being read. */
  cancelTask(cid: string): void {
    if (!send({ type: 'cancel_task', cid }))
      void api('/api/tasks/cancel', { method: 'POST', body: { cid } }).catch(() => {});
  },
  /** Cancel whatever is running now (the Cancel button in the command bar). */
  cancelCurrentTask(): void {
    if (!send({ type: 'cancel_task', cid: '' }))
      void api('/api/tasks/cancel', { method: 'POST', body: { scope: 'current' } }).catch(() => {});
  },
  setMic(active: boolean): void {
    send({ type: 'mic_toggle', active });
  },
  setStandby(on: boolean): void {
    void api('/api/standby', { method: 'POST', body: { on } }).catch(() => {});
  },
  selectModel(alias: string): void {
    send({ type: 'model_select', alias });
  },
  sendFile(file: File, onError: (msg: string) => void): void {
    const MAX = 25 * 1024 * 1024; // keep in sync with EraLive._MAX_UPLOAD_BYTES
    if (file.size > MAX) {
      onError(`"${file.name}" is larger than 25 MB.`);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      send({ type: 'file_upload', name: file.name, mime: file.type, data: result.slice(result.indexOf(',') + 1) });
    };
    reader.onerror = () => onError(`Could not read "${file.name}".`);
    reader.readAsDataURL(file);
  },
  shutdown(): Promise<unknown> {
    return api('/api/shutdown', { method: 'POST', body: { confirm: true } });
  },
  restart(): Promise<unknown> {
    return api('/api/restart', { method: 'POST', body: { confirm: true } });
  },
  reveal(cardId: string): Promise<unknown> {
    return api('/api/reveal', { method: 'POST', body: { cardId } });
  },
  /** Open a card's / source's file or folder, or a path shown in HUD text.
   *  Card/source paths come from the backend's own store; a raw path is only
   *  accepted if the HUD published it. A file that would run code is revealed. */
  openPath(ref: { cardId?: string; sourceId?: string; path?: string }): Promise<unknown> {
    return api('/api/reveal', { method: 'POST', body: { ...ref, mode: 'open' } });
  },
  /** Window controls exist only inside the legacy frameless Qt window. */
  windowControl(action: string): void {
    send({ type: 'window_control', action });
  },
};
