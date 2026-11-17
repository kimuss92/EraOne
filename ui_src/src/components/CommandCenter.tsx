import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode, type RefObject } from 'react';
import { GripVertical, Plus, X, Lock, Unlock, Maximize2, Cpu, Radio, CalendarDays, Brain, Globe, CloudSun, Layers, Radar, Activity, SlidersHorizontal, Palette, MoveDiagonal2 } from 'lucide-react';
import AppearancePanel from './AppearancePanel';
import type { CoreNode } from './core/constellation';
import { getState, setState, useHud, useNow } from '../lib/store';
import { api } from '../lib/transport';
import type { WidgetLayout } from '../lib/types';
import { dockPlacement, dropPlacement, isResultSurface, type Placement } from '../lib/hudPresentation';
import CoreStage from './CoreStage';
import ActivityPanel from './ActivityPanel';
import TelegramCallPanel from './TelegramCallPanel';
import ScheduledPanel from './ScheduledPanel';
import SystemPanel from './SystemPanel';
import ContextPanel, { MemoryPanel, SourcesPanel, WeatherSection } from './ContextPanel';
import ResultSurface from './ResultSurface';
import LiveTranscript from './LiveTranscript';
import AwarenessPanel from './AwarenessPanel';
import LayoutEditor from './LayoutEditor';

export async function editLayout(change: Record<string, unknown>): Promise<void> {
  const current = getState().widgetLayout;
  if (!current) return;
  try {
    const layout = await api<WidgetLayout>('/api/layout', { method: 'POST', body: { ...change, revision: current.revision } });
    if (layout.revision >= (getState().widgetLayout?.revision ?? -1)) setState({ widgetLayout: layout });
  } catch (error) {
    const layout = await api<WidgetLayout>('/api/layout');
    if (layout.revision >= (getState().widgetLayout?.revision ?? -1)) setState({ widgetLayout: layout });
    throw error;
  }
}

function WeatherClock() {
  const date = new Date(useNow() * 1000);
  return <div className="era-weather-clock"><div className="era-local-clock"><time>{date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time><span>{date.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })}</span></div><WeatherSection inline /></div>;
}

export default function CommandCenter({ onSchedule, memorySearchRef, nodes, corePaused = false }: { nodes: CoreNode[]; onSchedule: () => void; memorySearchRef: RefObject<HTMLInputElement | null>; corePaused?: boolean }) {
  const layout = useHud(s => s.widgetLayout);
  const online = useHud(s => s.link === 'online');
  const cards = useHud(s => s.cards);
  const call = useHud(s => s.telegramCall);
  const [arranging, setArranging] = useState(false);
  const [adding, setAdding] = useState(false);
  const [customizing, setCustomizing] = useState(false);
  const resizing = useRef<{ id: string; sx: number; sy: number; p: Placement } | null>(null);
  const [peek, setPeek] = useState<string | null>(null);
  const [selected, setSelected] = useState('core');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [dragged, setDragged] = useState('');
  const [preview, setPreview] = useState<Placement | null>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const dragOrigin = useRef({ x: 0, y: 0 });
  const editable = online && !busy && (layout?.schemaVersion ?? 0) >= 3;
  useEffect(() => { setDragged(''); setPreview(null); }, [layout?.revision, online]);
  const change = async (value: Record<string, unknown>) => {
    setBusy(true); setError('');
    try { await editLayout(value); }
    catch { setError('Position could not be saved. Latest layout restored; please try again.'); }
    finally { setBusy(false); }
  };
  const widgets: Record<string, ReactNode> = {
    core: <div className="era-core-stage"><CoreStage hero nodes={nodes} paused={corePaused} /><LiveTranscript /></div>,
    activity: <ActivityPanel />,
    calls: call ? <TelegramCallPanel /> : <div className="p-3 text-xs t2">Waiting for call state.<button className="mt-2 block ac-text" onClick={() => setState({ drawer: 'diagnostics' })}>Diagnostics</button></div>,
    schedule: <ScheduledPanel onSchedule={onSchedule} />,
    memory: <MemoryPanel searchRef={memorySearchRef} />,
    context: <ContextPanel />, sources: <SourcesPanel />, weather: <WeatherClock />,
    system: <SystemPanel />, awareness: <AwarenessPanel />,
  };
  const titles: Record<string, string> = { core: 'Sphere & conversation', awareness: 'System awareness', context: 'Intelligence', weather: 'Time & weather', activity: 'Live tasks' };
  const icons: Record<string, ReactNode> = { core: <Radio />, activity: <Activity />, calls: <Radio />, schedule: <CalendarDays />, system: <Cpu />, awareness: <Radar />, context: <Layers />, sources: <Globe />, memory: <Brain />, weather: <CloudSun /> };
  const ordered = layout?.widgets.map(w => w.id) ?? [];
  // v3 geometry is authoritative. Older running backends get the new default
  // presentation until restarted, with editing disabled rather than lost edits.
  const visible = (layout?.widgets ?? []).filter(w => w.visible || w.id === 'core').map(w => ({ ...w, ...(layout!.schemaVersion < 3 ? dockPlacement(w.id, ordered) : {}) }));
  const rows = Math.max(28, ...visible.map(w => w.y + w.h));
  const picked = visible.find(w => w.id === selected) ?? visible[0];
  const position = (p: Placement): CSSProperties => ({ left: `${p.x / 12 * 100}%`, top: `${p.y / rows * 100}%`, width: `calc(${p.w / 12 * 100}% - 10px)`, height: `calc(${p.h / rows * 100}% - 10px)` });
  const dropAt = (clientX: number, clientY: number) => {
    const widget = visible.find(w => w.id === dragged), rect = canvas.current?.getBoundingClientRect();
    if (!widget || !rect) return null;
    return dropPlacement(widget, (clientX - rect.left) / rect.width * 12 - dragOrigin.current.x, (clientY - rect.top) / rect.height * rows - dragOrigin.current.y);
  };
  // Corner-drag resize, snapped to the same 12-column grid and bounds the
  // backend validates (w 3–12, h 4–32). Saved on release like the editor.
  const resizedTo = (e: ReactPointerEvent): Placement | null => {
    const r = resizing.current, rect = canvas.current?.getBoundingClientRect();
    if (!r || !rect) return null;
    const w = Math.round(r.p.w + (e.clientX - r.sx) / rect.width * 12);
    const h = Math.round(r.p.h + (e.clientY - r.sy) / rect.height * rows);
    return { ...r.p, w: Math.max(3, Math.min(12 - r.p.x, w)), h: Math.max(4, Math.min(32, h)) };
  };
  const startResize = (e: ReactPointerEvent, widget: Placement & { id: string }) => {
    if (!editable) return;
    e.preventDefault(); e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    resizing.current = { id: widget.id, sx: e.clientX, sy: e.clientY, p: { x: widget.x, y: widget.y, w: widget.w, h: widget.h } };
    setSelected(widget.id);
  };
  const moveResize = (e: ReactPointerEvent) => { const p = resizedTo(e); if (p) setPreview(p); };
  const endResize = (e: ReactPointerEvent) => {
    const r = resizing.current, p = resizedTo(e);
    resizing.current = null; setPreview(null);
    if (r && p && (p.w !== r.p.w || p.h !== r.p.h)) void change({ widget: r.id, placement: p });
  };
  return <div className="era-cockpit era-unified era-free-layout">
    <div className="era-workspace-bar"><span className="era-eyebrow">{arranging ? 'ARRANGE YOUR WORKSPACE' : 'YOUR PERSONAL OPERATING SYSTEM'}</span><div className="era-workspace-actions">
      <button className={arranging ? 'selected' : ''} disabled={!editable} onClick={() => { setPeek(null); setArranging(!arranging); }}>{arranging ? <Unlock /> : <Lock />}{arranging ? 'Done' : 'Arrange'}</button>
      <button disabled={!online || busy} onClick={() => setAdding(!adding)}><Plus />Widgets</button>
      <button className={customizing ? 'selected' : ''} aria-expanded={customizing} onClick={() => setCustomizing(!customizing)}><Palette />Customize</button>
      <button disabled={!cards.some(isResultSurface)} onClick={() => window.dispatchEvent(new Event('era:show-result'))}><Layers />Last result</button>
    </div></div>
    {customizing && <AppearancePanel onClose={() => setCustomizing(false)} />}
    {layout && layout.schemaVersion < 3 && <p className="era-layout-notice">Restart Era One to enable the upgraded layout editor.</p>}
    {error && <p role="alert" className="era-layout-notice">{error}</p>}
    {arranging && <><p className="era-arrange-hint">Drag any header to move its panel, drag its corner to resize. Select a panel to set its exact position and size. Overlapping panels move down automatically.</p>
      {picked && <LayoutEditor widget={picked} title={titles[picked.id] || layout!.catalog[picked.id]} busy={!editable} save={change} />}</>}
    {adding && <div className="era-widget-picker">{layout?.widgets.filter(w => !w.visible && w.id !== 'core').map(w => <button key={w.id} disabled={busy || !online} onClick={() => void change({ widget: w.id, visible: true })}><Plus />{titles[w.id] || layout.catalog[w.id]}</button>)}{visible.length === layout?.widgets.length && <span>All panels are visible. Arrange lets you hide them.</span>}</div>}
    <div className="era-canvas-scroll"><div ref={canvas} className={`era-canvas ${arranging ? 'is-arranging' : ''}`} style={{ height: `${rows / 28 * 100}%` }}
      onDragOver={e => { if (!arranging || !editable || !dragged) return; e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setPreview(dropAt(e.clientX, e.clientY)); }}
      onDrop={e => { if (!arranging || !editable || !dragged) return; e.preventDefault(); e.stopPropagation(); const p = dropAt(e.clientX, e.clientY); const id = dragged; setDragged(''); setPreview(null); if (p) void change({ widget: id, placement: { x: p.x, y: p.y, w: p.w, h: p.h } }); }}>
      {visible.map(widget => <section key={widget.id} data-widget={widget.id} tabIndex={0} aria-label={titles[widget.id] || layout!.catalog[widget.id]}
        className={`era-widget era-widget-${widget.id} ${peek === widget.id ? 'is-peeking' : ''} ${dragged === widget.id ? 'is-moving' : ''} ${arranging && picked?.id === widget.id ? 'is-selected' : ''} ${widget.y >= 18 ? 'era-peek-up' : ''}`}
        style={{ ...position(widget), '--entry-x': widget.x < 3 ? '-18px' : widget.x >= 9 ? '18px' : '0px', '--entry-delay': `${80 + Math.min(widget.y,21)*10 + (widget.x >= 9 ? 70 : 0)}ms` } as CSSProperties} onClick={() => { if (arranging) setSelected(widget.id); }}
        onKeyDown={e => { if (e.key === 'Escape') { setPeek(null); e.currentTarget.blur(); } }}>
        <div className="era-widget-head"><span className="era-widget-grip" draggable={arranging && editable}
          onDragStart={e => {
            const rect = canvas.current!.getBoundingClientRect();
            dragOrigin.current = { x: (e.clientX - rect.left) / rect.width * 12 - widget.x, y: (e.clientY - rect.top) / rect.height * rows - widget.y };
            e.stopPropagation(); e.dataTransfer.setData('application/x-era-widget', widget.id); e.dataTransfer.effectAllowed = 'move'; setSelected(widget.id); setDragged(widget.id);
          }} onDragEnd={() => { setDragged(''); setPreview(null); }}>
          {arranging ? <GripVertical /> : icons[widget.id]}<span>{titles[widget.id] || layout!.catalog[widget.id]}</span></span>
          {arranging ? <div className="era-widget-controls"><button aria-label={`Position and resize ${widget.id}`} onClick={() => setSelected(widget.id)}><SlidersHorizontal /></button>
            {widget.id !== 'core' && <button aria-label={`Hide ${widget.id}`} disabled={!editable} onClick={() => void change({ widget: widget.id, visible: false })}><X /></button>}</div>
            : widget.id !== 'core' && <button className="era-peek-button" aria-label={`Expand ${widget.id}`} aria-expanded={peek === widget.id} onClick={() => setPeek(peek === widget.id ? null : widget.id)}><Maximize2 /></button>}
        </div><div className="era-widget-body custom-scrollbar">{widgets[widget.id]}</div>
        {arranging && editable && <span className="era-widget-resize" role="slider" aria-label={`Resize ${titles[widget.id] || layout!.catalog[widget.id]}`}
          aria-valuetext={`${widget.w} columns by ${widget.h} rows`} title="Drag to resize"
          onPointerDown={e => startResize(e, widget)} onPointerMove={moveResize} onPointerUp={endResize}
          onPointerCancel={() => { resizing.current = null; setPreview(null); }}><MoveDiagonal2 /></span>}
      </section>)}
      {preview && <div className="era-drop-preview" style={position(preview)} aria-hidden="true" />}
      <ResultSurface />
    </div></div>
  </div>;
}
