import { memo, useEffect, useState } from 'react';
import type { ActivityState, SystemMetrics } from '../hooks/useEra';

interface Props {
  metrics: SystemMetrics;
  activity: ActivityState;
  connected: boolean;
  standby: boolean;
}

const SPARK_LEN = 22;

// HUD OVERLAY — the floating technical chrome around the neural core:
// micro-labels, a live CPU sparkline, the connector line down to the command
// bar and the floor platform glow. Everything is pointer-events-none and
// colored through the ac-* accent utilities, so state changes recolor it via
// CSS with zero re-renders. Values are real where the backend provides them
// (CPU history, MEM capacity); SYNC and MS/STEP are derived readouts.
//
// GEOMETRY — every position below is expressed in --R (the sphere's radius)
// and --gap, both defined by .core-geom in index.css and mirrored by the canvas
// in NeuralCore. Tags anchor at 50% ± (--R + --gap), so they hug the sphere's
// edge rather than the column's, and they follow it as it grows and shrinks.
//
// RESPONSIVE LABELS — the tags used to be hidden outright below an 880px
// column, which is why the whole ring of readouts vanished the moment the
// window left full size. They now stay: .core-geom shrinks the sphere to open
// a margin, and each label swaps to a short form that fits it. Only below
// ~520px, where there is genuinely no band left to write in, do they go.
function HudOverlay({ metrics, activity, connected, standby }: Props) {
  // CPU sparkline history — one sample per system_metrics push (~1Hz).
  const [spark, setSpark] = useState<number[]>([]);
  useEffect(() => {
    if (metrics.cpu_percent === null) return;
    setSpark(prev => [...prev, metrics.cpu_percent as number].slice(-SPARK_LEN));
  }, [metrics.cpu_percent]);

  const cpu = metrics.cpu_percent ?? 0;
  const sync = connected ? Math.max(90, 99.8 - cpu * 0.05).toFixed(1) : null;
  const msStep = connected ? (8 + cpu * 0.12).toFixed(2) : null;
  const capacity = metrics.ram_percent !== null ? Math.round(metrics.ram_percent) : null;

  const coreStatus = !connected ? 'OFFLINE' : standby ? 'DORMANT' : 'ACTIVE';
  const matrixStatus = !connected ? 'LINK DOWN'
    : activity === 'thinking' || activity === 'processing' ? 'HIGH LOAD'
    : 'OPTIMAL';

  return (
    <div
      className="core-geom pointer-events-none absolute inset-0 z-0 overflow-hidden font-mono"
      aria-hidden="true"
    >
      {/* ── Floor platform + connector to the command bar ────────────── */}
      <div className="hud-floor absolute left-1/2 top-[calc(50%+var(--R)*0.78)] h-[calc(var(--R)*0.44)] w-[min(calc(var(--R)*2.1),80%)] -translate-x-1/2" />
      <div className="hud-floor-ring absolute left-1/2 top-[calc(50%+var(--R)*0.86)] h-[calc(var(--R)*0.22)] w-[min(calc(var(--R)*1.6),62%)] -translate-x-1/2 rounded-[100%]" />
      <div className="hud-floor-ring absolute left-1/2 top-[calc(50%+var(--R)*0.92)] h-[calc(var(--R)*0.14)] w-[min(calc(var(--R)*1.05),40%)] -translate-x-1/2 rounded-[100%] opacity-60" />
      <div className="hud-connector absolute bottom-10 left-1/2 top-[calc(50%+var(--R)*0.98)] w-px -translate-x-1/2" />
      <div className="hud-connector-node absolute bottom-[2.1rem] left-1/2 h-2.5 w-2.5 -translate-x-1/2 rounded-full" />

      {/* ── Side tags ───────────────────────────────────────────────────
          Anchored a --gap off the SPHERE's edge (50% ± --R), not the column
          edge, so they ride along as it shrinks and grows. */}
      <div className="hidden @[520px]:block">
      {/* ── Left flank (right edge just off the sphere) ─────────────── */}
      <div className="absolute right-[calc(50%+var(--R)+var(--gap))] top-[calc(50%-var(--R)*0.68)] flex flex-col items-end gap-1">
        <div className="hud-tag rounded-full px-1.5 py-0.5 text-[8px] tracking-[0.12em] @[880px]:px-2 @[880px]:py-1 @[880px]:text-[9px] @[880px]:tracking-[0.18em]">
          <span className="@[880px]:hidden">CORE</span>
          <span className="hidden @[880px]:inline">NEURAL CORE v1.1.3</span>
          <span className="t3 ml-1.5 @[880px]:ml-2">{coreStatus}</span>
        </div>
        <div className="hud-tick h-px w-6 @[880px]:w-10" />
      </div>

      <div className="absolute right-[calc(50%+var(--R)+var(--gap))] top-[calc(50%-var(--R)*0.05)] flex flex-col items-end gap-1 @[880px]:right-[calc(50%+var(--R)+var(--gap)*1.5)]">
        <div className="hud-tag rounded-full px-1.5 py-0.5 text-[8px] tracking-[0.12em] @[880px]:px-2 @[880px]:py-1 @[880px]:text-[9px] @[880px]:tracking-[0.18em]">
          SYNC <span className="ac-text-strong">{sync !== null ? `${sync}%` : '—'}</span>
        </div>
      </div>

      <div className="absolute right-[calc(50%+var(--R)+var(--gap))] top-[calc(50%+var(--R)*0.52)] flex flex-col items-end gap-1">
        <div className="hud-tick h-px w-8 @[880px]:w-14" />
        <div className="ac-label text-[7px] tracking-[0.18em] @[880px]:text-[8px] @[880px]:tracking-[0.25em]">
          <span className="@[880px]:hidden">MATRIX</span>
          <span className="hidden @[880px]:inline">NEURONAL MATRIX</span>
          <span className="t3 ml-1.5 @[880px]:ml-2">{matrixStatus}</span>
        </div>
      </div>

      {/* ── Right flank (left edge just off the sphere) ─────────────── */}
      <div className="absolute left-[calc(50%+var(--R)+var(--gap))] top-[calc(50%-var(--R)*0.74)] flex flex-col items-start gap-1">
        <div className="ac-label text-[7px] tracking-[0.18em] @[880px]:text-[8px] @[880px]:tracking-[0.25em]">ANALYSIS</div>
        <div className="hud-tag flex h-5 items-end gap-px rounded-full px-1.5 py-0.5 @[880px]:h-7 @[880px]:gap-[2px] @[880px]:px-2 @[880px]:py-1">
          {Array.from({ length: SPARK_LEN }).map((_, i) => {
            const v = spark[spark.length - SPARK_LEN + i] ?? spark[i];
            const h = v !== undefined ? Math.max(8, v) : 8;
            return (
              <span
                key={i}
                className="ac-fill w-px rounded-[1px] @[880px]:w-[3px]"
                style={{ height: `${h}%`, opacity: v !== undefined ? 0.9 : 0.25 }}
              />
            );
          })}
          <span className="ac-text-strong ml-1 text-[8px] tabular-nums @[880px]:ml-1.5 @[880px]:text-[9px]">
            {metrics.cpu_percent !== null ? `${Math.round(cpu)}%` : '—'}
          </span>
        </div>
      </div>

      <div className="absolute left-[calc(50%+var(--R)+var(--gap))] top-[calc(50%-var(--R)*0.05)] @[880px]:left-[calc(50%+var(--R)+var(--gap)*1.5)]">
        <div className="hud-tag rounded-full px-1.5 py-0.5 text-[8px] tracking-[0.1em] tabular-nums @[880px]:px-2 @[880px]:py-1 @[880px]:text-[9px] @[880px]:tracking-[0.15em]">
          {msStep !== null ? `${msStep} ms/step` : '— ms/step'}
        </div>
      </div>

      <div className="absolute left-[calc(50%+var(--R)+var(--gap))] top-[calc(50%+var(--R)*0.38)] flex flex-col items-start gap-1">
        <div className="hud-tick h-px w-8 @[880px]:w-14" />
        <div className="ac-label text-[7px] tracking-[0.18em] @[880px]:text-[8px] @[880px]:tracking-[0.25em]">
          <span className="@[880px]:hidden">STREAM</span>
          <span className="hidden @[880px]:inline">THOUGHT STREAM</span>
        </div>
      </div>

      <div className="absolute left-[calc(50%+var(--R)+var(--gap))] top-[calc(50%+var(--R)*0.66)]">
        <div className="hud-tag rounded-full px-1.5 py-0.5 text-[8px] tracking-[0.12em] @[880px]:px-2 @[880px]:py-1 @[880px]:text-[9px] @[880px]:tracking-[0.18em]">
          <span className="@[880px]:hidden">CAP</span>
          <span className="hidden @[880px]:inline">CAPACITY</span>
          <span className="ac-text-strong tabular-nums"> {capacity !== null ? `${capacity}%` : '—'}</span>
        </div>
      </div>
      </div>
    </div>
  );
}

export default memo(HudOverlay);
