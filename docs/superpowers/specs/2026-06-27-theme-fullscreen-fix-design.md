# ADR-003: Website Theme Disruption & Netflix Fullscreen Fix

## Status
Accepted

## Context
Two persistent bugs in JARVIS browser automation:

1. **Website theme disruption:** When JARVIS connects to the user's existing Brave browser via CDP (`connect_over_cdp`), certain websites experience a theme/reset flicker — dark/light mode preferences or CSS custom properties are reset.

2. **Netflix fullscreen misfire:** When `netflix_control` is called with `action="fullscreen"`, the native Netflix keyboard shortcut "f" triggers a **play/pause toggle** instead of entering fullscreen mode.

## Root Cause Analysis

### Bug 1: Theme Disruption
The root cause is `_restore_native_downloads()` in `actions/browser_control.py:476-484`. It opens a new CDP session on existing pages and sends `Browser.setDownloadBehavior` + `Page.setDownloadBehavior`, causing Chromium to re-evaluate page context and reset CSS/preference state. The `_on_new_page` handler fires for pages during the initial `connect_over_cdp` handshake.

### Bug 2: Netflix Fullscreen Misfire
In `_handle_fullscreen_real_click()`, Step 0 runs:
```javascript
const v = document.querySelector('video');
if (v) { v.focus(); v.click(); }
```
The `v.click()` triggers Netflix's video click handler which toggles play/pause — this is the misfire. `v.focus()` alone gives the video element DOM focus; `v.click()` is redundant and harmful.

## Decision

### Fix 1: Remove Harmful `v.click()` in Step 0
Change to:
```javascript
const v = document.querySelector('video');
if (v) { v.focus(); }
```

`v.focus()` alone is sufficient — the `<video>` element is natively focusable in Chromium. Subsequent CDP `page.keyboard.press("f")` dispatches keydown on the focused element, bubbling to Netflix's document-level keyboard handler.

### Fix 2: Guard `_restore_native_downloads` with `_cdp_ready` Flag
- `_BrowserSession.__init__`: `self._cdp_ready = False`
- `_launch()`: sets `self._cdp_ready = True` AFTER all existing pages registered
- `_on_new_page()`: checks `if not self._cdp_ready: return` to skip `_restore_native_downloads` for pages seen during initial connection

## Scope of Changes
| File | Change | Lines Affected |
|------|--------|---------------|
| `actions/netflix_control.py` | Remove `v.click()` from Step 0 | 1 line |
| `actions/browser_control.py` | Add `_cdp_ready` flag + guard + CDP count log | Already applied |

## Testing & Verification

### Static
- Compile check: `python -m py_compile` on both files — zero errors
- Test suite: `test_verify_changes.py` — all 40+ checks pass

### Runtime
1. **Theme fix:** Connect to CDP, verify log shows `CDP connected: N existing pages`
2. **Fullscreen fix:** Run `netflix_control action=fullscreen` on a watch page — no play/pause, enters fullscreen
3. **F11 fallback:** If both Tier 1 and Tier 2 fail, Tier 3 fires OS F11 as last resort

## References
- `actions/netflix_control.py` — `_handle_fullscreen_real_click` (line 4058)
- `actions/browser_control.py` — `_launch` (line 438), `_on_new_page` (line 486)
- `core/action_registry.py` — Netflix fullscreen guard (line 117)
- `core/self_healing_router.py` — Netflix fullscreen early-return (line 1242)
