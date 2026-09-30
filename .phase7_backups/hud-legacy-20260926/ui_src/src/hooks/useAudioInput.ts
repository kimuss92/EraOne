import { useCallback, useEffect, useRef, useState } from 'react';

declare global {
  interface Window {
    webkitAudioContext?: typeof AudioContext;
  }
}

export type AudioState = 'idle' | 'listening';

// Noise gate and smoothing constants
const NOISE_GATE = 0.02;
// Smoothing rates, per SECOND — not per frame. They were per-frame fractions
// (0.15 / 0.08), which silently made the meter's response depend on the
// display: the same speech settled ~2.4x faster on a 144Hz panel than on 60Hz,
// and any throttling of the loop slowed it further. These are the exponential
// rates that reproduce the old per-frame values EXACTLY at 60Hz
// (1 - e^(-9.75/60) = 0.15, 1 - e^(-5/60) = 0.08) and now hold that behaviour
// at any rate. Same dt-correctness rule NeuralCore follows for its own
// smoothing — a fixed per-frame fraction advances further on an uneven frame.
const ATTACK_RATE = 9.75;  // when volume increases
const RELEASE_RATE = 5.0;  // when volume decreases
const SPEAKING_HOLD_MS = 200;

// React state is only pushed at this rate (quantized), NOT per animation
// frame. Per-frame setState re-rendered the whole App at monitor refresh
// rate whenever the mic was on — the main cause of the UI glitch.
const STATE_PUSH_INTERVAL_MS = 80; // ~12.5 Hz
const QUANT = 100; // quantize volumes to 1/100 so equal values skip renders

/** Frame-rate-independent approach toward `target` (exponential decay). */
function approach(current: number, target: number, rate: number, dt: number): number {
  return current + (target - current) * (1 - Math.exp(-rate * dt));
}

interface UseAudioInputReturn {
  micActive: boolean;
  volume: number;
  peakVolume: number;
  isSpeaking: boolean;
  start: () => void;
  stop: () => void;
}

export function useAudioInput(): UseAudioInputReturn {
  const [micActive, setMicActive] = useState(false);
  const [volume, setVolume] = useState(0);
  const [peakVolume, setPeakVolume] = useState(0);
  const [isSpeaking, setIsSpeaking] = useState(false);

  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const rafRef = useRef<number>(0);
  const speakingTimerRef = useRef<number>(0);
  const smoothedVolumeRef = useRef(0);
  const lastPushRef = useRef(0);

  const cleanup = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    if (sourceRef.current) {
      try { sourceRef.current.disconnect(); } catch {}
      sourceRef.current = null;
    }
    if (analyserRef.current) {
      try { analyserRef.current.disconnect(); } catch {}
      analyserRef.current = null;
    }
    if (audioCtxRef.current && audioCtxRef.current.state !== 'closed') {
      try { audioCtxRef.current.close(); } catch {}
      audioCtxRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(t => t.stop());
      streamRef.current = null;
    }
    smoothedVolumeRef.current = 0;
    setVolume(0);
    setPeakVolume(0);
    setIsSpeaking(false);
  }, []);

  useEffect(() => () => cleanup(), [cleanup]);

  const start = useCallback(async () => {
    if (micActive) return;
    try {
      // Request mic with noise suppression and echo cancellation
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: false,
        }
      });
      streamRef.current = stream;

      const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext;
      const ctx = new AudioContextClass();
      audioCtxRef.current = ctx;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.75;
      analyserRef.current = analyser;
      const source = ctx.createMediaStreamSource(stream);
      source.connect(analyser);
      sourceRef.current = source;

      const data = new Uint8Array(analyser.frequencyBinCount);
      smoothedVolumeRef.current = 0;
      // Start the gate from now, not from a timestamp left by a previous
      // mic session, so the first dt is a real frame interval.
      lastPushRef.current = performance.now();

      // The whole analysis is gated to STATE_PUSH_INTERVAL_MS, not just the
      // React push. It used to run the FFT read plus a 128-bin reduction on
      // every animation frame — up to 240Hz on a high-refresh panel — to feed a
      // meter that can only ever show ~12.5Hz. Everything below the gate was
      // computed and thrown away. Same gating idea as NeuralCore's
      // FRAME_INTERVAL; the smoothing above is dt-based, so slowing the loop no
      // longer slows the meter's response.
      const tick = () => {
        rafRef.current = requestAnimationFrame(tick);

        const now = performance.now();
        const elapsed = now - lastPushRef.current;
        if (elapsed < STATE_PUSH_INTERVAL_MS) return;
        // Clamp dt so a stall (hidden window, GC pause) can't jump the
        // smoothing straight to the target in one step.
        const dt = Math.min(0.25, elapsed / 1000);
        lastPushRef.current = now;

        analyser.getByteFrequencyData(data);
        let sum = 0;
        let peak = 0;
        for (let i = 0; i < data.length; i++) {
          sum += data[i];
          if (data[i] > peak) peak = data[i];
        }
        const avg = sum / data.length / 255;

        // Noise gate: ignore values below threshold
        const gated = avg > NOISE_GATE ? avg : 0;

        // Smooth with attack/release (frame-rate-independent)
        const rate = gated > smoothedVolumeRef.current ? ATTACK_RATE : RELEASE_RATE;
        smoothedVolumeRef.current = approach(smoothedVolumeRef.current, gated, rate, dt);

        // Quantized state push — React only re-renders when a visibly
        // different value lands.
        const qVol = Math.round(smoothedVolumeRef.current * QUANT) / QUANT;
        const qPeak = Math.round((peak / 255) * QUANT) / QUANT;
        setVolume(prev => (prev === qVol ? prev : qVol));
        setPeakVolume(prev => (prev === qPeak ? prev : qPeak));

        // Derive speaking state with hold release
        if (smoothedVolumeRef.current > NOISE_GATE) {
          setIsSpeaking(true);
          window.clearTimeout(speakingTimerRef.current);
          speakingTimerRef.current = window.setTimeout(() => setIsSpeaking(false), SPEAKING_HOLD_MS);
        }
      };
      rafRef.current = requestAnimationFrame(tick);

      setMicActive(true);
    } catch (err) {
      console.error('Mic access denied', err);
      setMicActive(false);
    }
  }, [micActive]);

  const stop = useCallback(() => {
    cleanup();
    setMicActive(false);
  }, [cleanup]);

  return { micActive, volume, peakVolume, isSpeaking, start, stop };
}
