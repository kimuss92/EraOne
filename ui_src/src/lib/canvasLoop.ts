/** Wake near the next draw, rather than polling a throttled canvas at 60 Hz.
 * A null interval pauses the loop; refresh after changing that condition. */
export function createCanvasLoop(element: Element, draw: (now: number) => void, interval: () => number | null = () => 0) {
  let frame = 0, timer = 0, visible = true, disposed = false, last = -Infinity;
  const cancel = () => { cancelAnimationFrame(frame); window.clearTimeout(timer); frame = 0; timer = 0; };
  const awake = () => !disposed && !document.hidden && visible && interval() !== null;
  const schedule = (now: number) => {
    if (!awake()) return;
    // Leave half a display frame for rAF alignment; long idle intervals sleep.
    const delay = Math.max(0,(interval() ?? 0) - (now-last) - 8);
    if (delay > 0) timer = window.setTimeout(() => { timer = 0; if (awake()) frame = requestAnimationFrame(tick); },delay);
    else frame = requestAnimationFrame(tick);
  };
  const tick = (now: number) => {
    frame = 0;
    if (!awake()) return;
    if (now-last >= (interval() ?? 0)-2) { last = now; draw(now); }
    schedule(now);
  };
  const sync = () => {
    cancel(); last = -Infinity;
    if (awake()) frame = requestAnimationFrame(tick);
  };
  const observer = new IntersectionObserver(entries => { visible = entries[0]?.isIntersecting ?? false; sync(); });
  observer.observe(element); document.addEventListener('visibilitychange',sync); sync();
  return { refresh: sync, stop: () => { disposed = true; cancel(); observer.disconnect(); document.removeEventListener('visibilitychange',sync); } };
}

export function startCanvasLoop(element: Element, draw: (now: number) => void, interval?: () => number | null): () => void {
  return createCanvasLoop(element,draw,interval).stop;
}
