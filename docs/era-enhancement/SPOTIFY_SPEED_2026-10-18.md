# Spotify favorite speed repair

Later October 18 update: source now loaded by normal restart; playback state
also reads all output devices, fixing a native default-only false negative.
Read `STABILITY_RELEASE_2026-10-18.md` for current full/focused verification and
startup evidence. Favorite voice/physical acceptance remains owed. Historical
source-restart notes below describe this repair's earlier checkpoint.

## Findings and plan before implementation

Owner clarification: **no Spotify API**. This installation uses the desktop app.
The new path requires no OAuth or bridge setup; existing optional integrations
remain compatible but are not enabled or exercised in native acceptance.

The title/artist favorite is saved, but no Spotify identity is saved. October 16
search took 30,033 ms after a first UI worker timeout and one protected retry.
Existing exact-ID Web API control and the native bridge are already implemented.
The bridge is disabled, no bridge ports are listening, and the diagnostic child
has no OAuth environment token. A saved ID currently reaches an unavailable
transport message instead of the working desktop client.

Public Spotify metadata matches the user's saved title and artist:
`https://open.spotify.com/track/4lOgmIWfsaTu8nj4JWW2qw`,
`spotify:track:4lOgmIWfsaTu8nj4JWW2qw`, 5:21.720, Mikasa Music.
Official album metadata: `https://open.spotify.com/album/3tYDfLy1D7QhlfVFqfb7s4`.
The album's one track has that identity. Evidence was read without account
cookies, credential discovery or playback. Exa search was rate-limited; the
available web search and public Spotify embed metadata resolved the recording.

Authorized implementation:

1. Extend the existing strict Spotify identity parser to accept ordinary HTTPS
   share links (including locale prefixes), URI and raw IDs. Canonicalize locally,
   reject other hosts/schemes/types and never follow arbitrary URLs.
2. Carry the saved title/artist alongside a saved favorite ID for desktop
   verification. Preserve family-member exclusions and title fallback.
3. Reuse API first and enabled bridge second. Only when the API is unconfigured
   and the bridge disabled, open the canonical track URI once through Spotify's
   existing Windows protocol handler in the existing killable worker. No UIA,
   pixel estimation, account changes,
   integration installation or generic media-key fallback.
4. Require observed matching title/artist and active Spotify audio before success.
   If the requested metadata is loaded but paused, send one explicit Spotify Play
   (never toggle), with a 250ms SendMessageTimeout. Bound verification,
   launch-worker lifetime and cancellation. Do not repeat the URI or
   perform a name search after an ambiguous outcome. Native GSMTC does not expose
   the exact Spotify ID, so describe this as metadata/playback verification.
5. Add meaningful regressions for hostile URLs, exact native dispatch, metadata
   mismatch, paused playback, launch failure, timeout/cancellation, no replay and
   existing post-play media routing/minimization. Keep all UIA in the existing
   killable worker and preserve its context/search behavior and single retry.
6. Save the verified favorite URI through existing long-term memory only after
   the repaired route is tested. Run protected suites, scoped Ruff, manifest
   checks and a bounded native playback test; report measured limits honestly.

No UI/build or Claude billing changes. A source restart is required for the
running Era process to load the new route. Do not claim a sub-500ms cold start.

## Results — October 18

Implemented in `actions/spotify_control.py`, `actions/spotify_search_worker.py`,
`core/spotify_api.py` (local identity parser only), `core/personal_media.py` and
two regression files. Existing optional API support was not enabled. Protected
search/primary UI worker branches and their retry remain unchanged; compared
against this session's backup, not the much older Git baseline. Backup:
`.phase7_backups/spotify-speed-20261018-130612/`.

One real desktop playback requested the verified URI, changing from a different
song to **سواد الغيم — Mikasa Music**. It returned verified success in **3,194ms**;
Spotify audio was active on **pc - speakers (Realtek(R) Audio)**, the remembered
media target. The diagnostic child explicitly removed its API token environment
variable; no API setup, account changes or bridge enablement. It preserved Era's
own voice selection. Existing post-action minimization ran. Evidence:
`runtime/spotify-speed-native-result-20261018.json`.

This one measurement is much faster than the earlier 30,033ms name-search run,
but is not a controlled benchmark, cold-start guarantee or sub-500ms claim.
Native verification observes title/artist and active audio, not the Spotify
internal media ID. Physical listening and repeated voice-command acceptance are
not proven by these observations. Worker launch is bounded to 1.5s; verification
uses a 6s warm / 12s cold deadline, with an in-flight bounded metadata read able
to finish after that deadline. Failure/cancellation does not replay the URI.

After native success, existing `memory.memory_manager.update_memory` saved
`preferences.favorite_song_uri` and the owner's desktop/no-API preference. A
read-back preserved the original title and resolved “play my favorite song” to
the exact URI plus saved metadata. No new memory store or cloud backup.

Verification: **287 passed, 2 warnings** across identity/native dispatch,
existing search/worker, personal memory, directive, speech, routing, approvals,
scheduler authority, toolspec and hidden-window suites. Required six suites are
included. Scoped Ruff check/format and compilation passed. Manifest regenerated
and freshness checked; **8 manifest regression tests passed** separately.
Logs: `runtime/spotify-speed-final-tests-20261018.log`. These are selected suites,
not a full repository run. Known unrelated audio/crew failures were not changed.

The running Era process was not restarted. After restart, still owed: voice
“play my favorite song” from this memory, first-try audible correctness and
remembered routing, Liked Songs / Release Radar, isolated cancellation and
successful exact-action approval. See TODO #43 / #45. No HUD source/build,
tool-declaration, prompt or protected speech changes in this repair.
