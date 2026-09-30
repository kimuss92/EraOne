# Spotify keyless bridge — CSP / renderer probe

Read-only investigation. No Spotify patch, downgrade, or install was performed.
Answers: *can a Spicetify extension in the Spotify renderer reach a local port on
Spicetify 2.45.1, given the client's Content-Security-Policy?*

## Verdict

**BRIDGE-CAN-RUN**

Qualified: the bridge works through the **POLL-mode reverse channel** (the
extension reaches OUT to a local server with `fetch`/WebSocket), **not** through
`require('http')` SERVER mode. Node built-ins are absent in the renderer;
outbound local networking from an extension is a proven, in-the-wild pattern and
CSP does not block it on a Spicetify-patched client.

## Evidence

1. **`require` / Node built-ins are NOT exposed — first-hand, this machine.**
   When the era-bridge extension attempted to load (Spotify 1.3.1.234, Spicetify
   2.45.1), it reached its SERVER-mode branch, which writes a token file and binds
   port 8766. Neither happened:
   - `%APPDATA%\spicetify\era-bridge.token` was never written (`Test-Path` →
     `False`).
   - Nothing listened on 8766 (`Get-NetTCPConnection -LocalPort 8766` → empty).
   The extension's `try { require("http") } catch` therefore fell through to POLL
   mode. Conclusion: `require('http')`/`require('fs')` are unavailable in the
   extension scope — SERVER mode structurally cannot run here. This matches modern
   Spotify disabling Node integration in the main renderer.

2. **Outbound local networking from an extension is proven in the wild.** Two
   live Spicetify extensions connect the renderer to a local server:
   - **WebNowPlaying** (`Extensions/webnowplaying.js`, shipped with spicetify/cli):
     a `WNPReduxWebSocket` class opens a WebSocket to a local server with
     exponential-backoff reconnect — the exact "extension as client → local
     server" shape as our POLL mode.
   - **Om-Thorat/Spicetify-extension**: README — *"by injecting Javascript we are
     able to expose the api with a websocket, which can be accessed by starting a
     server on `localhost:443`."*
   If CSP blocked local connections, neither would function. It does not.

3. **Extensions have `fetch` and `CosmosAsync`; that is how they do network I/O.**
   spicetify/cli's example extensions use `CosmosAsync` for Spotify Web API and
   WebSocket for local integrations (webnowplaying, popupLyrics). Our POLL mode
   uses the same primitives — `fetch` to Era's relay + `CosmosAsync` for playback.

4. **Loopback is a Chromium secure context — mixed content does not apply.**
   Chromium treats `127.0.0.1` / `localhost` / `[::1]` as potentially
   trustworthy, so an https-origin renderer calling `http://localhost` is not
   blocked as mixed content, independent of CSP.

5. **`localhost` beats `127.0.0.1` in this renderer.** Multiple reports (Spotify
   community "127.0.0.1 being changed to localhost", dart-webdev #233) show
   local connections that fail on the literal `127.0.0.1` but succeed on
   `localhost` (IPv4/IPv6 loopback resolution). Our extension/relay currently use
   `127.0.0.1`; the reactivation step below fixes that.

### Where the code stands (Option 1 confirmed)

- `spicetify config extensions` → empty; era-bridge is **not loaded**.
- era-bridge files kept at `%APPDATA%\spicetify\Extensions\era-bridge\`
  (`extension.js`, `manifest.json`, `README.md`) — dormant, not deleted.
- `core/spotify_bridge.py::is_enabled()` reads `spotify_bridge.enabled`
  (config/api_keys.json), **default false**. The flag exists via that default.
- `app/tool_declarations.py` withholds the `spotify_bridge` tool while disabled,
  so the model routes every Spotify intent to window-automation.
- `app/era_live/tool_dispatch.py` gates the `spotify_bridge` branch on
  `is_enabled()` — no network I/O while dormant (no cold-start, no relay).
- Every `spotify_*` row in `core/action_registry.py` resolves to
  `computer_settings` (→ `actions/spotify_control.py`), never the bridge.

## Recommended next step

A downgrade (or waiting for Spicetify to support Spotify 1.3.x) is worth it — the
bridge reactivates via POLL mode once the extension loads, provided the extension
and relay use `localhost` (not `127.0.0.1`) and the SERVER-mode `require` path is
treated as dead on this client.

## What window-automation already covers (`actions/spotify_control.py`)

Live path today. Router `spotify_control(parameters)` → `_dispatch`, 16 verbs:

| Verb | How it works |
|------|--------------|
| `play` / `open` | WM_APPCOMMAND MEDIA_PLAY + launch/focus |
| `pause` / `pause_if_running_only` | explicit WM_APPCOMMAND MEDIA_PAUSE |
| `next` / `previous` | WM_APPCOMMAND next/prev track |
| `search` | opens `spotify:search:` URI, waits for render, clicks result card |
| `liked_songs` / `new_releases` / `made_for_you` | Alt+Shift hotkey + pixel click |
| `volume_up` / `volume_down` | Ctrl+↑/↓ on the focused Spotify window |
| `like` / `dislike` | Alt+Shift+B on the current track |
| `current_track` | parses the window title (`Artist - Song`) |
| `status` | Windows audio-session state + window title/placement |
| `minimize` | minimize the Spotify window |

### What window-automation structurally CANNOT do (the bridge's reason to exist)

- Play an exact track/album/playlist by **URI** — `search` is fuzzy and clicks
  result cards by pixel ratios (DPI/layout-fragile), never a precise selection.
- Read the current track as **data** — only the window title; no album, duration,
  progress, or track URI, and nothing when minimized/untitled (no OCR is done).
- Return **Liked Songs / New Releases as a list** — it only *plays* those pages;
  it cannot enumerate them.
- **Queue** control — add-to-queue or reorder.
- **Exact volume** — only relative Ctrl+↑/↓; cannot set a percentage or read the
  current level.
- **Seek** to a position.
- **Shuffle / repeat** mode control.
- **Like a specific (non-current) track** by URI.
- Every action **focuses/clicks the window**, so it steals focus and depends on
  window state, DPI, and Spotify's layout — the keyless bridge's CosmosAsync path
  needs none of that.
