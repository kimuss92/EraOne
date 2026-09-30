import { memo, useEffect, useRef, useState } from 'react';

interface VoiceMeterProps {
  volume: number;
  peakVolume: number;
  micActive: boolean;
  userVoiceActive: boolean;
  /** Bars only, no card — for sitting INSIDE the command pill next to the mic
   *  button. The card's labels ("VOICE ACTIVITY", "Mic ON • click to mute") are
   *  dropped there because the pill already says both: the mic button's own
   *  colour is the on/off state, and clicking it is the toggle. */
  compact?: boolean;
}

function VoiceMeter({ volume, peakVolume, micActive, userVoiceActive, compact = false }: VoiceMeterProps) {
  const [dimBars, setDimBars] = useState(false);
  const dimTimerRef = useRef<number>(0);

  // Dim bars when local volume is high but Python VAD says no real voice
  useEffect(() => {
    if (micActive && volume > 0.05 && !userVoiceActive) {
      // Start dim timer - if VAD still says no voice after 300ms, dim bars
      dimTimerRef.current = window.setTimeout(() => {
        setDimBars(true);
      }, 300);
    } else {
      // Clear dim state when VAD confirms voice or volume drops
      window.clearTimeout(dimTimerRef.current);
      setDimBars(false);
    }
    return () => window.clearTimeout(dimTimerRef.current);
  }, [micActive, volume, userVoiceActive]);

  const barsOpacity = dimBars ? 0.3 : 1.0;

  const bars = (count: number) =>
    Array.from({ length: count }).map((_, i) => {
      const threshold = (i + 1) / count;
      const active = micActive && volume > threshold * 0.75;
      const isPeak = active && peakVolume > threshold * 0.9;
      return (
        <div
          key={i}
          className={`h-full w-1 rounded-sm transition-all duration-75 ${
            active ? (isPeak ? 'ac-bar-peak' : 'ac-bar') : 'bg-[var(--track)]'
          }`}
          style={{ height: `${20 + i * 2}%` }}
        />
      );
    });

  if (compact) {
    return (
      <div
        className="flex h-11 shrink-0 items-center gap-[3px] px-1"
        style={{ opacity: barsOpacity, transition: 'opacity 300ms ease-out' }}
        title={micActive ? 'Mic ON' : 'Mic OFF'}
      >
        {bars(18)}
      </div>
    );
  }

  return (
    <div className="pointer-events-auto mb-1 flex flex-col gap-1.5 rounded-2xl glass px-4 py-3">
      <div className="t2 flex items-center justify-between gap-3 text-[10px] font-semibold uppercase tracking-wider">
        <span>Voice Activity</span>
        <span className={micActive ? 'ac-text' : 't3'}>
          {micActive ? 'LIVE' : 'OFF'}
        </span>
      </div>
      <div className="flex h-5 items-center gap-[3px]" style={{ opacity: barsOpacity, transition: 'opacity 300ms ease-out' }}>
        {bars(28)}
      </div>
      <div className="t3 text-[9px]">
        {micActive ? 'Mic ON • click to mute' : 'Mic OFF • click to unmute'}
        {dimBars && micActive && (
          <span className="ml-2 text-amber-500/70">[noise ignored]</span>
        )}
      </div>
    </div>
  );
}

// Memoized: only re-renders when the (throttled, quantized) volume changes.
export default memo(VoiceMeter);
