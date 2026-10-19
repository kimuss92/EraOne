import { useEffect, useState, type RefObject } from 'react';
import { useReducedMotion } from './motion';
import type { DrawerId } from './types';

export function useDrawerTransition(drawer: DrawerId, reduced: boolean) {
  const [last, setLast] = useState(drawer);
  useEffect(() => {
    if (drawer || reduced) { setLast(drawer); return; }
    const timer = window.setTimeout(() => setLast(null),220);
    return () => window.clearTimeout(timer);
  }, [drawer,reduced]);
  return { visible: reduced ? drawer : drawer || last, leaving: !reduced && !drawer && last !== null };
}

export function panelTilt(x: number, y: number, rect: { left: number; top: number; width: number; height: number }) {
  if (![x,y,rect.left,rect.top,rect.width,rect.height].every(Number.isFinite) || rect.width <= 0 || rect.height <= 0) return { x: 0, y: 0 };
  const clamp = (v: number) => Math.max(-1,Math.min(1,v));
  return { x: -clamp((y-rect.top)/rect.height*2-1)*2, y: clamp((x-rect.left)/rect.width*2-1)*2 };
}

export function canvasInterval(state: string, active: boolean, reduced: boolean, frameMs: number) {
  const base = reduced ? active ? 100 : 500 : state === 'standby' || state === 'offline' ? 100 : active ? 1000/60 : 1000/30;
  return Math.max(base, frameMs > 20 ? 1000/12 : frameMs > 12 ? 1000/20 : 0);
}

export function useCinematicInteraction(ref: RefObject<HTMLElement | null>, enabled = true) {
  const reduced = useReducedMotion();
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const visibility = () => { root.dataset.documentHidden = String(document.hidden); };
    visibility(); document.addEventListener('visibilitychange',visibility);
    return () => document.removeEventListener('visibilitychange',visibility);
  }, [ref]);
  useEffect(() => {
    const root = ref.current;
    const fine = window.matchMedia('(hover: hover) and (pointer: fine)');
    if (!root || reduced || !enabled) return;
    let panel: HTMLElement | null = null, core: HTMLElement | null = null;
    let rect: DOMRect | null = null, coreRect: DOMRect | null = null;
    let frame = 0, x = 0, y = 0;
    const reset = () => {
      if (frame) cancelAnimationFrame(frame); frame = 0;
      panel?.classList.remove('is-pointer-lit'); panel?.style.removeProperty('--tilt-x'); panel?.style.removeProperty('--tilt-y');
      core?.removeAttribute('data-pointer-near');
      panel = null; core = null; rect = null; coreRect = null;
      delete root.dataset.panelFocus;
    };
    const move = (event: PointerEvent) => {
      if (event.pointerType !== 'mouse' || !fine.matches || document.hidden) { reset(); return; }
      const target = event.target as HTMLElement;
      const nextPanel = target.closest<HTMLElement>('.era-widget:not(.era-widget-core),.rail > .panel');
      const nextCore = target.closest<HTMLElement>('.era-neural-stage');
      if (nextPanel?.closest('.is-arranging,.is-peeking,[inert]') || nextCore?.closest('[inert]')) { reset(); return; }
      if (panel !== nextPanel || core !== nextCore) {
        reset(); panel = nextPanel; core = nextCore;
        if (panel) { rect = panel.getBoundingClientRect(); panel.classList.add('is-pointer-lit'); root.dataset.panelFocus = 'true'; }
        if (core) coreRect = core.getBoundingClientRect();
      }
      x = event.clientX; y = event.clientY;
      if (frame || (!panel && !core)) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (panel && rect) {
          const tilt = panelTilt(x,y,rect);
          panel.style.setProperty('--tilt-x',`${tilt.x.toFixed(2)}deg`);
          panel.style.setProperty('--tilt-y',`${tilt.y.toFixed(2)}deg`);
        }
        if (core && coreRect) core.dataset.pointerNear = String(Math.hypot(x-coreRect.left-coreRect.width/2,y-coreRect.top-coreRect.height/2+10) < Math.min(coreRect.width,coreRect.height)*.4);
      });
    };
    root.addEventListener('pointermove',move,{passive:true});
    root.addEventListener('pointerleave',reset);
    root.addEventListener('scroll',reset,true);
    window.addEventListener('blur',reset); window.addEventListener('resize',reset);
    document.addEventListener('visibilitychange',reset);
    fine.addEventListener('change',reset);
    return () => {
      reset(); root.removeEventListener('pointermove',move); root.removeEventListener('pointerleave',reset); root.removeEventListener('scroll',reset,true);
      window.removeEventListener('blur',reset); window.removeEventListener('resize',reset);
      document.removeEventListener('visibilitychange',reset); fine.removeEventListener('change',reset);
    };
  }, [ref,reduced,enabled]);
}
