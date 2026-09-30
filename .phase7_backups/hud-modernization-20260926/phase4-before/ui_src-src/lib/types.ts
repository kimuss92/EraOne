// Shared HUD contracts. Mirrors app/hud/events.py (AgentEvent, states),
// app/hud/tool_adapter.py (ContextCard, sources) and app/hud/status.py.

export type AgentState =
  | 'idle'
  | 'wake_detected'
  | 'listening'
  | 'transcribing'
  | 'routing'
  | 'thinking'
  | 'tool_running'
  | 'synthesizing'
  | 'speaking'
  | 'interrupted'
  | 'complete'
  | 'error'
  | 'offline'
  | 'standby'
  | 'awaiting_approval';

export interface AgentEvent {
  id: string;
  seq: number;
  ts: number;
  sessionId: string;
  type: string;
  state?: string;
  label?: string;
  message?: string;
  toolName?: string;
  toolCallId?: string;
  duration?: number;
  metadata?: Record<string, unknown>;
}

export type ToolStatus =
  | 'queued'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled'
  | 'blocked'
  | 'awaiting'
  | 'unknown';

/** Era's single pending confirmation, mirrored by app/hud/bus_approval.py.
 *  Never raw parameters: `title` is the same safe one-liner Activity shows. */
export interface Approval {
  id: string;
  tool: string;
  label: string;
  title: string;
  category: string;
  reason: string;
  risk: string;
  createdAt: number;
  expiresAt: number;
}

export interface ToolRun {
  id: string;
  tool: string;
  label: string;
  category: string;
  activity: string;
  args: Record<string, unknown>;
  status: ToolStatus;
  startedAt: number;
  finishedAt?: number;
  duration: number | null;
  summary: string;
  background: boolean;
  source: string;
  routedTo?: string;
}

export interface ContextCard {
  id: string;
  type: string;
  title: string;
  subtitle: string;
  icon: string;
  payload: Record<string, unknown>;
  source: string;
  timestamp: number;
  actions: { id: string; label: string }[];
}

export interface Source {
  id: string;
  type: string;
  title: string;
  subtitle?: string;
  url?: string;
  query?: string;
  origin?: string;
  toolCallId?: string;
  timestamp: number;
}

export interface ConversationEntry {
  id: string;
  role: 'user' | 'agent' | 'system';
  text: string;
  ts: number;
  level?: string;
  source?: string;
}

export interface TimelineItem {
  id: string;
  ts: number;
  kind: string;
  label: string;
  detail?: string;
  duration?: number;
  tone: 'info' | 'ok' | 'warn' | 'bad';
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
  os?: string;
}

export interface ActiveModel {
  label: string;
  alias?: string;
  detail?: string;
  fellBackFrom?: string;
  fallbackReason?: string;
  pinned?: boolean;
}

export interface ModelChoice {
  alias: string;
  label: string;
  detail: string;
  role: string;
  available: boolean;
  unavailableReason: string;
  costPerCallUsd: number;
  realtimeAudio: boolean;
}

export interface Latency {
  ttftMs: number | null;
  firstAudioMs: number | null;
  responseMs: number | null;
  transcriptionMs: number | null;
  ttftAvgMs?: number | null;
  firstAudioAvgMs?: number | null;
  responseAvgMs?: number | null;
  transcriptionAvgMs?: number | null;
  tools?: Record<string, number>;
}

export interface ConnectionInfo {
  kind: string;
  label: string;
  status: string;
  since?: number;
}

export interface Transcript {
  user: string;
  userFinal: boolean;
  response: string;
  responding: boolean;
}

export type DrawerId = 'conversation' | 'diagnostics' | 'settings' | 'capabilities' | null;

export interface HudSettings {
  weather_city: string;
  hud_sounds: boolean;
  open_hud_on_start: boolean;
  app_mode: boolean;
  autostart?: { enabled: boolean; startup_shortcut: boolean; scheduled_task: string | null };
}
