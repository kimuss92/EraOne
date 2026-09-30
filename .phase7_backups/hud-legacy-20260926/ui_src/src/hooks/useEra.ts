import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react';

export type ActivityState = 'idle' | 'listening' | 'thinking' | 'processing' | 'speaking' | 'standby';

export interface LogEntry {
  id: string;
  role: 'user' | 'agent' | 'system';
  text: string;
  time: number;
}

export interface SystemMetrics {
  cpu_percent: number | null;
  ram_percent: number | null;
  ram_used_gb: number | null;
  ram_total_gb: number | null;
  net_sent_bytes_per_sec: number | null;
  net_recv_bytes_per_sec: number | null;
  wifi_connected: boolean | null;
  gpu_usage_percent: number | null;
  gpu_temp_celsius: number | null;
  gpu_mem_used_mb: number | null;
  gpu_mem_total_mb: number | null;
  uptime_seconds: number | null;
  process_count: number | null;
}

/** Which model handled the last request. Mirrors RouteDecision on the Python
 *  side (core/llm/router.py) — the fallback fields are what let the UI show a
 *  displaced choice instead of quietly presenting a substitute as the original. */
export interface ActiveModel {
  label: string;
  alias?: string;
  detail?: string;
  fellBackFrom?: string;
  fallbackReason?: string;
  pinned?: boolean;
}

/** One option in the model switcher. Mirrors ModelRouteMixin.model_catalog()
 *  (app/era_live/model_route.py), which builds it from config/llm.yaml — so a
 *  new alias appears here without a frontend edit. */
export interface ModelChoice {
  alias: string;
  label: string;
  detail: string;
  role: string;
  available: boolean;
  unavailableReason: string;
  /** Measured cost of making a call at all. Non-zero for the Claude tiers, and
   *  shown in the menu: pinning one of those makes the next message a paid
   *  call, and a switcher that hides that spends money quietly. */
  costPerCallUsd: number;
  realtimeAudio: boolean;
}

interface UseEraReturn {
  connected: boolean;
  activity: ActivityState;
  muted: boolean;
  logs: LogEntry[];
  activeModel: ActiveModel | null;
  /** What the switcher can offer. Empty until the backend publishes it. */
  modelCatalog: ModelChoice[];
  /** The pinned alias, or '' for automatic routing. Carried separately from
   *  activeModel.alias because a pin that FELL BACK reports a different alias
   *  as active — ticking that one would tell the user they chose it. */
  selectedModel: string;
  maximized: boolean;
  browserMicActive: boolean;
  metrics: SystemMetrics;
  userVoiceActive: boolean;
  /** Live amplitude of Era's own voice (0..1). Mutable ref, updated on every
   *  WS amplitude frame WITHOUT re-rendering React — the sphere's rAF loop
   *  reads it directly. (Was React state: re-rendered the whole App per frame
   *  while Era spoke.) */
  agentAmplitudeRef: MutableRefObject<number>;
  /** Per-band energies (low → high) of the SAME voice frame, 0..1. This is what
   *  lets the sphere behave as an equalizer rather than just pulsing: the rAF
   *  loop maps each band to a latitude ring of the shell. Ref for the same
   *  reason as the amplitude — it updates ~33Hz and must not re-render React. */
  agentBandsRef: MutableRefObject<Float32Array>;
  sendCommand: (text: string) => void;
  sendFile: (file: File) => void;
  toggleMute: () => void;
  sendWindowControl: (action: string) => void;
  sendMicToggle: (active: boolean) => void;
  sendMicVolume: (volume: number) => void;
  /** Pin a model by alias, or '' to go back to automatic. */
  selectModel: (alias: string) => void;
}

// Keep in sync with EraLive._MAX_UPLOAD_BYTES in main.py.
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

// Keep in sync with _EQ_BANDS in main.py's _play_audio.
export const EQ_BANDS = 8;

// `?ws=ws://127.0.0.1:8766` points the dev server at a stand-in backend, so UI
// work does not require launching Era — and, more to the point, does not
// require stopping a running one to free the port. No param means the normal
// address, so production behaviour is unchanged.
const DEFAULT_WS_URL = 'ws://127.0.0.1:8765';
const WS_URL =
  new URLSearchParams(window.location.search).get('ws') || DEFAULT_WS_URL;
// Persistent conversation: keep a long scrollback so the full discussion stays
// visible (entries no longer fade — see App.tsx). Raised from 100.
const MAX_LOG = 500;

function uid(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
}

const EMPTY_METRICS: SystemMetrics = {
  cpu_percent: null,
  ram_percent: null,
  ram_used_gb: null,
  ram_total_gb: null,
  net_sent_bytes_per_sec: null,
  net_recv_bytes_per_sec: null,
  wifi_connected: null,
  gpu_usage_percent: null,
  gpu_temp_celsius: null,
  gpu_mem_used_mb: null,
  gpu_mem_total_mb: null,
  uptime_seconds: null,
  process_count: null,
};

export function useEra(): UseEraReturn {
  const [connected, setConnected] = useState(false);
  const [activity, setActivity] = useState<ActivityState>('idle');
  const [muted, setMuted] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [maximized, setMaximized] = useState(false);
  const [browserMicActive, setBrowserMicActive] = useState(true);
  const [metrics, setMetrics] = useState<SystemMetrics>(EMPTY_METRICS);
  const [userVoiceActive, setUserVoiceActive] = useState(false);
  const [activeModel, setActiveModel] = useState<ActiveModel | null>(null);
  const [modelCatalog, setModelCatalog] = useState<ModelChoice[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const agentAmplitudeRef = useRef(0);
  const agentBandsRef = useRef<Float32Array>(new Float32Array(EQ_BANDS));
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number>(0);

  const addLog = useCallback((role: LogEntry['role'], text: string) => {
    setLogs(prev => {
      const next = [...prev, { id: uid(), role, text, time: Date.now() }];
      return next.slice(-MAX_LOG);
    });
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    try {
      const ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        setConnected(true);
        addLog('system', 'Connected to ERA ONE backend.');
      };

      ws.onclose = () => {
        setConnected(false);
        setActiveModel(null);
        agentAmplitudeRef.current = 0;
        agentBandsRef.current.fill(0);
        wsRef.current = null;
        reconnectTimerRef.current = window.setTimeout(connect, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };

      ws.onmessage = (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data);
          switch (data.type) {
            case 'state': {
              const s: string = data.state;
              if (s === 'LISTENING') setActivity('listening');
              else if (s === 'SPEAKING') setActivity('speaking');
              else if (s === 'THINKING') setActivity('thinking');
              else if (s === 'PROCESSING') setActivity('processing');
              else if (s === 'STANDBY') setActivity('standby');
              else if (s === 'MUTED') { setActivity('idle'); setMuted(true); }
              // Any non-speaking state means Era is silent — settle the sphere.
              if (s !== 'SPEAKING') {
                agentAmplitudeRef.current = 0;
                agentBandsRef.current.fill(0);
              }
              break;
            }
            case 'amplitude': {
              // Real amplitude of Era's own voice (0..1) — drives the sphere.
              // Ref write only: no React re-render, the sphere polls it per frame.
              agentAmplitudeRef.current = Math.max(0, Math.min(1, Number(data.value) || 0));
              // Spectrum for the equalizer. Written in place so the sphere's
              // loop keeps reading one stable Float32Array and we allocate
              // nothing on a 33Hz path.
              const bands = data.bands;
              const target = agentBandsRef.current;
              if (Array.isArray(bands)) {
                for (let i = 0; i < target.length; i++) {
                  const v = Number(bands[i]);
                  target[i] = Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0;
                }
              }
              break;
            }
            case 'log': {
              const text: string = data.text;
              if (text.startsWith('SYS:') || text.startsWith('WARN:') || text.startsWith('ERR:')) {
                addLog('system', text);
              } else if (/^You:\s*/i.test(text)) {
                addLog('user', text.replace(/^You:\s*/i, ''));
              } else if (/^(era one|era):\s*/i.test(text)) {
                // Backend emits "era: ..." for spoken replies — label it as the
                // agent (violet), not SYSTEM. (Was only matching "Era One:".)
                addLog('agent', text.replace(/^(era one|era):\s*/i, ''));
              } else {
                addLog('system', text);
              }
              break;
            }
            case 'mute': {
              setMuted(Boolean(data.muted));
              if (data.muted) setActivity('standby');
              else setActivity('listening');
              break;
            }
            case 'window_state': {
              setMaximized(Boolean(data.maximized));
              break;
            }
            case 'browser_mic': {
              setBrowserMicActive(Boolean(data.active));
              break;
            }
            case 'system_metrics': {
              if (data.data) {
                setMetrics(prev => ({ ...prev, ...data.data }));
              }
              break;
            }
            case 'user_voice_active': {
              setUserVoiceActive(Boolean(data.active));
              break;
            }
            case 'model': {
              // Which model served the last request. Kept as state (not a ref)
              // because it changes at conversation pace and must re-render the
              // indicator; the fallback fields ride along so the UI can show a
              // displaced choice rather than silently presenting a substitute.
              setActiveModel({
                label: String(data.label || ''),
                alias: data.alias ? String(data.alias) : undefined,
                detail: data.detail ? String(data.detail) : undefined,
                fellBackFrom: data.fell_back_from ? String(data.fell_back_from) : undefined,
                fallbackReason: data.fallback_reason ? String(data.fallback_reason) : undefined,
                pinned: Boolean(data.pinned),
              });
              break;
            }
            case 'model_catalog': {
              // The switcher's options, published on connect and again after
              // every pin change. Deliberately NOT merged into the 'model'
              // message: that one fires on every routing decision, and
              // re-sending the whole menu at conversation pace would re-render
              // the dropdown while the user is reading it.
              const rows = Array.isArray(data.models) ? data.models : [];
              setModelCatalog(
                rows.map((m: Record<string, unknown>) => ({
                  alias: String(m.alias || ''),
                  label: String(m.label || ''),
                  detail: String(m.detail || ''),
                  role: String(m.role || ''),
                  available: m.available !== false,
                  unavailableReason: String(m.unavailable_reason || ''),
                  costPerCallUsd: Number(m.cost_per_call_usd) || 0,
                  realtimeAudio: Boolean(m.realtime_audio),
                })).filter((m: ModelChoice) => m.alias),
              );
              setSelectedModel(String(data.selected || ''));
              break;
            }
            case 'theme': {
              // Voice/backend theme switch ("era, light mode"). Re-emitted as
              // a DOM event so App owns the single theme state — this hook
              // stays presentation-agnostic.
              window.dispatchEvent(new CustomEvent('era:theme', { detail: String(data.theme || 'toggle') }));
              break;
            }
          }
        } catch {
          // Ignore malformed messages
        }
      };

      wsRef.current = ws;
    } catch {
      reconnectTimerRef.current = window.setTimeout(connect, 5000);
    }
  }, [addLog]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  const sendCommand = useCallback((text: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'command', text }));
      addLog('user', text);
    }
  }, [addLog]);

  // Ships the actual file bytes to the backend — browsers never expose a
  // dropped/selected file's real filesystem path, so without this Era only
  // ever saw the bare filename and had nothing real to act on.
  const sendFile = useCallback((file: File) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      addLog('system', `"${file.name}" is too large to upload (${(file.size / (1024 * 1024)).toFixed(1)}MB, limit ${MAX_UPLOAD_BYTES / (1024 * 1024)}MB).`);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      const base64 = result.slice(result.indexOf(',') + 1); // strip "data:<mime>;base64," prefix
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'file_upload', name: file.name, mime: file.type, data: base64 }));
      }
    };
    reader.onerror = () => {
      addLog('system', `Could not read "${file.name}" for upload.`);
    };
    reader.readAsDataURL(file);
    addLog('user', `[uploaded] ${file.name}`);
  }, [addLog]);

  const toggleMute = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'mute', muted: !muted }));
    }
  }, [muted]);

  const sendWindowControl = useCallback((action: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'window_control', action }));
    }
  }, []);

  const sendMicToggle = useCallback((active: boolean) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      // Send the ABSOLUTE desired state the caller wants, not a blind toggle.
      // The backend applies it and echoes `browser_mic` back — that echo is the
      // single source of truth every client (indicator, meter mic) syncs to.
      // This kills the inversion bug where the indicator and the backend held
      // opposite states and each click re-inverted them.
      wsRef.current.send(JSON.stringify({ type: 'mic_toggle', active }));
    }
  }, []);

  const sendMicVolume = useCallback((volume: number) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'mic_volume', volume }));
    }
  }, []);

  // Send the alias and wait for the backend to echo the result, exactly as the
  // mic toggle does. Setting `selectedModel` optimistically here would show a
  // tick on a model that the router may have refused or fallen back from —
  // the one thing this whole indicator exists to prevent.
  const selectModel = useCallback((alias: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'model_select', alias }));
    }
  }, []);

  return { connected, activity, muted, logs, activeModel, modelCatalog, selectedModel, maximized, browserMicActive, metrics, userVoiceActive, agentAmplitudeRef, agentBandsRef, sendCommand, sendFile, toggleMute, sendWindowControl, sendMicToggle, sendMicVolume, selectModel };
}
