"""Netflix player control, driven through Netflix's own player API.

WHY NOT THE <video> ELEMENT
---------------------------
The first version of this layer started every script with
`document.querySelector('video')`. It silently did nothing, and the user
reported forward/rewind/volume "executing" with no change on screen.

Probing the live page over CDP showed why: `document.querySelector('video')`
returned **null** while Netflix's own API reported a healthy player at 322s of
a 3816s title. Netflix does not keep a stable, reachable `<video>` node in the
main document — so a script that begins there finds nothing, returns its
"no active media" sentinel, and the user is told the command ran.

Netflix exposes its real player at:

    netflix.appContext.state.playerApp.getAPI().videoPlayer
        .getVideoPlayerBySessionId(<session id>)

which provides seek / play / pause / setVolume / setMuted / getCurrentTime /
getDuration / playNextEpisode / setTextTrack / skipAd, verified present on the
live player. `setVolume()` was confirmed working end to end (1.0 -> 0.4).

So: **the API is the primary path and the `<video>` element is the fallback.**

VERIFY, DON'T ASSUME
--------------------
Seeking can be accepted and do nothing when the playback session has died —
exactly what happened behind Netflix error M7375, where the API still reported
a position but the video element was gone. Every seek therefore re-reads the
position afterwards and reports what actually happened. Era saying "done" when
nothing moved is the failure this closes.

Times: the Netflix API works in MILLISECONDS; `video.currentTime` is seconds.
"""

from __future__ import annotations

import json

# Resolve Netflix's player, with a video-element fallback. `_pos`/`_dur` return
# SECONDS from whichever source is available, so the callers stay unit-safe.
_PREAMBLE = """
const _api = () => {
  try {
    const vp = netflix.appContext.state.playerApp.getAPI().videoPlayer;
    const ids = vp.getAllPlayerSessionIds();
    if (!ids || !ids.length) return null;
    const pl = vp.getVideoPlayerBySessionId(ids[0]);
    return (pl && typeof pl.seek === 'function') ? pl : null;
  } catch (e) { return null; }
};
const _vid = () => {
  const vs = Array.from(document.querySelectorAll('video'));
  if (!vs.length) return null;
  vs.sort((a, b) => (b.duration || 0) - (a.duration || 0));
  return vs[0];
};
// Seconds, from the API when available, else the element.
const _pos = () => {
  const p = _api();
  if (p) { try { return p.getCurrentTime() / 1000; } catch (e) {} }
  const v = _vid();
  return v ? v.currentTime : null;
};
const _dur = () => {
  const p = _api();
  if (p) { try { return p.getDuration() / 1000; } catch (e) {} }
  const v = _vid();
  return v && isFinite(v.duration) ? v.duration : null;
};
const _uia = (...names) => {
  for (const n of names) {
    const el = document.querySelector('[data-uia="' + n + '"]');
    if (el) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return el;
    }
  }
  return null;
};
const _sel = (s) => {
  const el = document.querySelector(s);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return (r.width > 0 && r.height > 0) ? el : null;
};
const _click = (el) => {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const cx = Math.round(r.left + r.width / 2);
  const cy = Math.round(r.top + r.height / 2);
  for (const t of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
    try {
      const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
      el.dispatchEvent(new C(t, {bubbles: true, cancelable: true, clientX: cx, clientY: cy}));
    } catch (e) {}
  }
  el.click();
  return true;
};
// Netflix hides the control bar until the pointer moves; some controls cannot
// be found or clicked while it is hidden.
const _wake = () => {
  const stage = document.querySelector('.watch-video, [data-uia="player"], body');
  if (!stage) return;
  const r = stage.getBoundingClientRect();
  for (const t of ['pointermove', 'mousemove']) {
    try {
      const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
      stage.dispatchEvent(new C(t, {
        bubbles: true, cancelable: true,
        clientX: Math.round(r.left + r.width / 2),
        clientY: Math.round(r.top + r.height / 2),
      }));
    } catch (e) {}
  }
};
const _fmt = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const m = Math.floor(s / 60), sec = s % 60;
  return m + 'm' + (sec < 10 ? '0' : '') + sec + 's';
};
// Seek ONLY through Netflix's own player, then CHECK that it moved.
//
// ⛔ NEVER assign to `video.currentTime` on Netflix. Netflix streams through
// Media Source Extensions, and writing currentTime directly on an MSE element
// desynchronises the buffer from the player's state machine. That is what
// killed the playback session and produced Netflix error M7375 ("Veuillez nous
// excuser pour cette interruption") every time the user asked to rewind or
// fast-forward — confirmed by the user, and the reason this fallback is gone.
// If the player API is unreachable, we report it and stop. A refused seek is
// recoverable; a corrupted stream needs a reload and loses the user's place.
const _seekVerified = (targetSec, label) => {
  const p = _api();
  if (!p) {
    return Promise.resolve('I cannot reach the Netflix player to seek. '
      + 'The tab may need a reload.');
  }
  const before = _pos();
  if (before === null) return Promise.resolve('No active media context');
  try { p.seek(Math.round(targetSec * 1000)); }
  catch (e) { return Promise.resolve('Netflix refused the seek (' + e.message + ')'); }
  return new Promise(resolve => setTimeout(() => {
    const after = _pos();
    if (after === null) return resolve('No active media context');
    if (Math.abs(after - before) < 1 && Math.abs(after - targetSec) > 2) {
      return resolve('Netflix did not move the playback position — the player '
                     + 'session looks stalled and the tab needs a reload (still at '
                     + _fmt(after) + ')');
    }
    resolve(label + ' — now at ' + _fmt(after));
  }, 700));
};
// True when Netflix itself reports a playback error (the M7375 family).
const _playerError = () => {
  const p = _api();
  if (!p || typeof p.getError !== 'function') return null;
  try {
    const e = p.getError();
    if (!e) return null;
    return (e.errorCode || e.code || e.message || 'playback error').toString().slice(0, 60);
  } catch (e) { return null; }
};
"""


def _skip_script(uia: tuple[str, ...], css: tuple[str, ...], ok: str) -> str:
    """Intro / recap skip.

    These are genuinely button-only: Netflix exposes no API for intro or recap
    bounds (`getChapters()` is empty and `getTimeCodes()` gives only content
    start and the ending marker — verified live). So an ad-blocking extension
    that strips the skip button leaves nothing to fall back to. When the button
    is absent we say so plainly instead of returning a "not found" sentinel the
    user cannot act on — skipping the intro then means using the scrubber or a
    short forward seek.
    """
    uia_args = ", ".join(json.dumps(n) for n in uia)
    css_args = ", ".join(json.dumps(c) for c in css)
    return f"""
{_PREAMBLE}
_wake();
let btn = _uia({uia_args});
if (!btn) {{ for (const c of [{css_args}]) {{ btn = _sel(c); if (btn) break; }} }}
if (!btn) {{
  const any = Array.from(document.querySelectorAll('button')).find(b => {{
    const t = (b.textContent || '') + ' ' + (b.getAttribute('data-uia') || '');
    const r = b.getBoundingClientRect();
    return /skip|passer/i.test(t) && r.width > 0 && r.height > 0;
  }});
  if (any) btn = any;
}}
if (!btn) return 'The skip button is not on screen right now (an extension may be '
                + 'hiding it) — say "forward 90 seconds" to jump past it instead';
_click(btn);
return {json.dumps(ok)};
"""


_SKIP_TARGETS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "skip_intro": (
        ("player-skip-intro", "player-skip-intro-button"),
        (".watch-video--skip-content-button", ".skip-credits"),
        "Intro skipped",
    ),
    "skip_recap": (
        ("player-skip-recap", "player-skip-recap-button"),
        (".watch-video--skip-content-button",),
        "Recap skipped",
    ),
    # skip_credits is NOT here: it means "go to the next episode", which the API
    # does via playNextEpisode (works when the button is stripped). It is
    # dispatched through _next_episode_script() in build_player_script instead.
}


# Returned when the in-page attempts ran but the episode did not change. The
# Python side (netflix_control) then tries a trusted Playwright click / the N
# shortcut — synthetic in-page clicks and the API can both be silently ignored.
NEXT_EPISODE_UNMOVED = "NEXT_EPISODE_UNMOVED"
NEXT_EPISODE_OK = "Next episode starting"


def _next_episode_script() -> str:
    """Advance one episode, and only say so once the episode actually changed.

    `playNextEpisode()` exists on the player, but on 2026-09-26 it returned
    without error and did nothing — twice — while Era said "Next episode
    starting". Clicking the "Next episode (N)" button is what worked. So each
    step now waits for the episode id (URL /watch/<id> or the player's movie id)
    to change before reporting success.
    """
    return f"""
{_PREAMBLE}
const _ep = () => {{
  const m = location.pathname.match(/\\/watch\\/(\\d+)/);
  let mid = null;
  const p = _api();
  if (p && typeof p.getMovieId === 'function') {{ try {{ mid = p.getMovieId(); }} catch (e) {{}} }}
  return (m ? m[1] : '') + '|' + (mid || '');
}};
const _moved = (before, ms) => new Promise(resolve => {{
  const t0 = Date.now();
  const tick = () => {{
    if (_ep() !== before) return resolve(true);
    if (Date.now() - t0 > ms) return resolve(false);
    setTimeout(tick, 250);
  }};
  tick();
}});
const _nextBtn = () => {{
  _wake();
  const b = _uia('next-episode-seamless-button', 'next-episode-seamless-button-draining',
                 'control-next', 'nextEpisode', 'player-next-episode');
  if (b) return b;
  return Array.from(document.querySelectorAll('button, a, [role=button]')).find(el => {{
    const t = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim();
    const r = el.getBoundingClientRect();
    return /next episode|episode suivant|\\u00e9pisode suivant/i.test(t) && r.width > 0 && r.height > 0;
  }}) || null;
}};
const before = _ep();
if (before === '|') return 'No active media context';
return (async () => {{
  const p = _api();
  if (p && typeof p.playNextEpisode === 'function') {{
    try {{ p.playNextEpisode(); }} catch (e) {{}}
    if (await _moved(before, 3000)) return {json.dumps(NEXT_EPISODE_OK)};
  }}
  const btn = _nextBtn();
  if (btn) {{
    _click(btn);
    if (await _moved(before, 4000)) return {json.dumps(NEXT_EPISODE_OK)};
  }}
  return {json.dumps(NEXT_EPISODE_UNMOVED)};
}})();
"""


def _previous_episode_script() -> str:
    """Go back an episode.

    Netflix's player API has `playNextEpisode` but NO previous equivalent, and
    the web player ships no previous-episode button — which is why the tool
    honestly reported it unavailable. The workable route is the episode list,
    so open it and say so rather than pretending a control exists.
    """
    return f"""
{_PREAMBLE}
_wake();
let btn = _uia('control-previous', 'previousEpisode', 'player-previous-episode');
if (btn) {{ _click(btn); return 'Previous episode starting'; }}
const labelled = Array.from(document.querySelectorAll('button, a')).find(b => {{
  const t = ((b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
  const r = b.getBoundingClientRect();
  return /previous episode|\\u00e9pisode pr/i.test(t) && r.width > 0 && r.height > 0;
}});
if (labelled) {{ _click(labelled); return 'Previous episode starting'; }}
const list = _uia('control-episodes', 'episodes-list', 'player-episodes');
if (list) {{
  _click(list);
  return 'Netflix has no previous-episode control, so I opened the episode list — '
       + 'tell me which episode you want';
}}
return 'Netflix has no previous-episode button in the player. Say "show the episodes" '
     + 'and I will open the list';
"""


def _seek_script(delta_seconds: float | None, absolute_seconds: float | None) -> str:
    """Absolute or relative seek, clamped to the title and then verified."""
    if absolute_seconds is not None:
        target = f"Math.min(Math.max({float(absolute_seconds)}, 0), (_dur() || 1e9))"
        label = json.dumps("Jumped")
        return f"""
{_PREAMBLE}
if (_pos() === null) return 'No active media context';
return _seekVerified({target}, {label});
"""

    d = float(delta_seconds or 10)
    target = f"Math.min(Math.max(base + ({d}), 0), (_dur() || 1e9))"
    label = json.dumps(("Forward " if d >= 0 else "Back ") + f"{abs(d):g}s")
    return f"""
{_PREAMBLE}
const base = _pos();
if (base === null) return 'No active media context';
return _seekVerified({target}, {label});
"""


def _volume_script(level: float | None, delta: float | None) -> str:
    """Netflix's OWN volume (0-1), never the system mixer."""
    if level is not None:
        expr = f"Math.min(Math.max({float(level)}, 0), 1)"
    else:
        expr = f"Math.min(Math.max(cur + ({float(delta or 0.1)}), 0), 1)"
    return f"""
{_PREAMBLE}
const p = _api();
const v = _vid();
if (!p && !v) return 'No active media context';
let cur = 1;
if (p) {{ try {{ cur = p.getVolume(); }} catch (e) {{}} }}
else if (v) {{ cur = v.volume; }}
const target = {expr};
if (p) {{
  try {{ p.setVolume(target); if (target > 0 && typeof p.setMuted === 'function') p.setMuted(false); }}
  catch (e) {{}}
}}
if (v) {{ try {{ v.volume = target; if (target > 0) v.muted = false; }} catch (e) {{}} }}
return 'Netflix volume ' + Math.round(target * 100) + '%';
"""


_STATIC_SCRIPTS: dict[str, str] = {
    "restart": f"""
{_PREAMBLE}
if (_pos() === null) return 'No active media context';
const p = _api();
if (p) {{ try {{ p.play(); }} catch (e) {{}} }}
else {{ const v = _vid(); if (v && v.paused) v.play().catch(() => {{}}); }}
return _seekVerified(0, 'Restarted from the beginning');
""",
    # No API opens the in-player episode PANEL, and an ad-blocking extension
    # (e.g. Nikflix) strips the control button — so if the button is gone,
    # navigate to the title's own page, where Netflix always renders the
    # episode selector. Confirmed against the live player: only ~9 data-uia
    # nodes survive the extension, the episodes button among the casualties.
    "episodes": f"""
{_PREAMBLE}
_wake();
const btn = _uia('control-episodes', 'episodes-list', 'player-episodes');
if (btn) {{ _click(btn); return 'Episode list opened'; }}
const p = _api();
let mid = null;
if (p && typeof p.getMovieId === 'function') {{ try {{ mid = p.getMovieId(); }} catch (e) {{}} }}
if (mid) {{
  setTimeout(() => {{ location.href = 'https://www.netflix.com/title/' + mid; }}, 40);
  return 'Opening the episode list on the title page';
}}
return 'Episode list control element not found';
""",
    # Subtitles/audio: the in-player MENU button is stripped by ad-blocking
    # extensions, so drive Netflix's text-track API directly, which does not
    # need the button. `setTimedTextVisibility` toggle verified live under the
    # extension (True -> False). Falls back to the button when the API is absent.
    "subtitles": f"""
{_PREAMBLE}
const p = _api();
if (p && typeof p.setTimedTextVisibility === 'function') {{
  let on = false;
  try {{ on = p.isTimedTextVisible ? p.isTimedTextVisible() : false; }} catch (e) {{}}
  try {{
    p.setTimedTextVisibility(!on);
    return on ? 'Subtitles hidden' : 'Subtitles shown';
  }} catch (e) {{}}
}}
_wake();
const btn = _uia('control-audio-subtitle', 'player-audio-subtitle', 'control-subtitles');
if (btn) {{ _click(btn); return 'Subtitle and audio menu opened'; }}
return 'Subtitle control not available';
""",
    "back": """
if (document.fullscreenElement) { try { document.exitFullscreen(); } catch (e) {} }
const btn = document.querySelector('[data-uia="control-back"], [data-uia="back-button"]');
if (btn) { btn.click(); return 'Left the player'; }
window.location.href = 'https://www.netflix.com/browse';
return 'Returned to Netflix browse';
""",
    # Recover a stalled/errored player without losing the user's place: note
    # the position, reload, and Netflix resumes from its own bookmark.
    "reload": f"""
{_PREAMBLE}
const pos = _pos();
const err = _playerError();
const where = pos === null ? '' : ' Resuming from about ' + _fmt(pos) + '.';
setTimeout(() => {{ location.reload(); }}, 50);
return (err ? 'Netflix reported a playback error (' + err + '). Reloading.'
            : 'Reloading the Netflix player.') + where;
""",
    "playback_info": f"""
{_PREAMBLE}
const err = _playerError();
if (err) return 'Netflix is reporting a playback error (' + err + '). '
               + 'The tab needs a reload.';
const pos = _pos();
if (pos === null) return 'No active media context';
const dur = _dur();
const p = _api();
let paused = null;
if (p) {{ try {{ paused = p.getPaused(); }} catch (e) {{}} }}
if (paused === null) {{ const v = _vid(); paused = v ? v.paused : null; }}
const title = (document.querySelector('[data-uia="video-title"]')?.textContent || '').trim();
return (title ? title + ' \\u2014 ' : '')
     + (paused === null ? 'loaded' : (paused ? 'paused' : 'playing'))
     + ' at ' + _fmt(pos)
     + (dur ? ' of ' + _fmt(dur) + ', ' + _fmt(dur - pos) + ' remaining' : '');
""",
}


def build_player_script(action: str, params: dict | None = None) -> str | None:
    """Return the script for `action`, or None when this module doesn't own it."""
    p = params or {}

    if action in _SKIP_TARGETS:
        return _skip_script(*_SKIP_TARGETS[action])
    if action == "skip_credits":
        # "Skip the credits" = go to the next episode: same verified path.
        return _next_episode_script()
    if action == "next_episode":
        return _next_episode_script()
    if action == "previous_episode":
        return _previous_episode_script()
    if action in _STATIC_SCRIPTS:
        return _STATIC_SCRIPTS[action]

    if action in ("forward", "rewind"):
        amount = _coerce_float(p.get("amount") or p.get("seconds"), 10.0)
        amount = max(1.0, min(abs(amount), 3600.0))
        return _seek_script(amount if action == "forward" else -amount, None)

    if action == "seek":
        target = _coerce_float(p.get("position") or p.get("seconds") or p.get("amount"), None)
        if target is None:
            return None
        return _seek_script(None, max(0.0, target))

    if action in ("volume_up", "volume_down"):
        step = _coerce_float(p.get("amount"), 10.0)
        step = max(1.0, min(abs(step), 100.0)) / 100.0
        return _volume_script(None, step if action == "volume_up" else -step)

    if action == "set_volume":
        level = _coerce_float(p.get("value") or p.get("level") or p.get("amount"), None)
        if level is None:
            return None
        # Accept 0-1 and 0-100 alike: "set Netflix volume to 30" means 30%.
        level = level / 100.0 if level > 1 else level
        return _volume_script(max(0.0, min(level, 1.0)), None)

    return None


def _coerce_float(value, default):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


PLAYER_ACTIONS: frozenset[str] = frozenset(
    set(_SKIP_TARGETS)
    | set(_STATIC_SCRIPTS)
    | {
        "skip_credits",  # left _SKIP_TARGETS for the API path — keep it listed
        "next_episode",
        "previous_episode",
        "forward",
        "rewind",
        "seek",
        "volume_up",
        "volume_down",
        "set_volume",
    }
)
