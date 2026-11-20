# Application launch repair — 2026-10-13

## Observed causes

The user showed a Telegram uninstall confirmation after requesting app opening.
The current launcher returned the first Start Menu shortcut whose filename
contained the app name. Native read-only inspection found both Telegram.lnk and
Désinstaller Telegram.lnk; the latter targets unins000.exe. Selection depended
on directory enumeration order and did not inspect the executable or arguments.

The Brave registry branch independently opened about:blank in the default browser,
instead of executing the Brave path it had just resolved. The final fallback typed
into Windows Search and pressed Enter without inspecting the selected result.
The open_app verifier unconditionally marked successful-looking results VERIFIED.
Generic close handling occurred after launch branches, so it could open an app
instead of closing it.

## Design and applied scope

This is an incremental Phase 6 repair under the approved task-reliability plan and
the user's explicit app-launch request. Alternatives were a filename-only blacklist,
ranked inspected launch targets, or continued visual shell search. Ranked target
inspection is selected: filename filtering alone misses misleading shortcut names,
and blind shell selection cannot establish executable identity.

- Rank exact shortcut names before qualified matches; reject maintenance names,
  uninstall executables, maintenance arguments and shell-interpreter targets.
- Read .lnk target/arguments through Windows COM and inspect again before launch.
  Equal-ranked links to different binaries are ambiguous and are not guessed.
- Cache only the Start Menu path inventory for at most roughly one minute;
  target existence/safety is checked each use. No full-disk executable crawl.
- Focus an existing window by app process identity before resolving a potentially
  stale installation. Browser tabs and uninstall process captions are not app proof.
- Launch the requested registry executable, inspected shortcut or executable PATH
  match. Resolve Store apps from registered Get-StartApps package IDs when needed.
  The fixed discovery command contains no user-interpolated PowerShell code.
- Remove blind Start-menu keystroke selection from the execution path.
- Process close/toggle intent before opening. Apply cancellation at launch/wait
  boundaries and propagate the worker token using offload. Slow window/catalog
  checks detach after 1.5 seconds through the existing background-reporting path.
- Observe the application window before claiming it opened; report pending window
  confirmation and denied foreground focus honestly. The verifier reads actual
  process-owned windows instead of automatically trusting launch messages.
- Preserve explicit Spotify playback behavior; plain opening does not request play.

## Native evidence and remaining installation issue

Brave resolves to the valid local BraveSoftware executable. Both Brave and Telegram
running windows were found by process identity in a read-only probe. Resolution
and window observation took about 79 ms for Brave and 100 ms for Telegram in the
final probe; these are observations, not guaranteed cold-launch latency.

Telegram's launch and uninstall links point to D:\Users\nesri\AppData\Roaming\
Telegram Desktop. File-existence checks in this Codex session report both targets
unavailable, although the running Telegram process reports the same executable
location and installation metadata also references it. The reason this session
cannot see that target is not established. Running Telegram can be identified
without resolving that path. Closed-app launch from the inaccessible location
remains unverified; no shortcut rewrite, drive change, installation or removal was
performed, and no alternate executable location was invented.

## Verification

Initial focused vendor/launch/cancellation run: 155 passed, with two existing
dependency warnings. Additional process-identity, misleading shortcut-target and
already-running cases were added. Final expanded system-awareness gate: 324 passed
in 15.78 seconds. Speech gate: 682 passed in 32.43 seconds, with existing Google SDK
deprecation and pywinauto COM initialization warnings. Verification layer: 31 passed;
main tool bridge: 127 passed. Doctor: zero errors/zero warnings, READY.

Scoped Ruff, Python compilation and whitespace checks passed. Initial import-order
and unused-import lint findings were repaired. The first native shortcut probe
reported a COM-proxy release warning; releasing the shortcut proxy before apartment
teardown removed it in the final native read-only probe.

Tests simulate launch/focus/close calls; native probes only read shortcuts, process
identities and windows. No live app launch, uninstall dialog interaction, physical
focus change, Era restart or full global-suite rerun was performed.

## Limits and next work

This repair covers safe app opening and the existing close/toggle path, not universal
control of every app. Hidden/tray-only windows, inaccessible process identity,
unregistered/ambiguous apps and some custom launchers may remain unconfirmed.
There is no automatic install, repair, uninstall, or alternative destructive action.
Store catalog discovery has a five-second subprocess timeout; an already executing
native operation cannot be forcibly interrupted. Live launch acceptance, other
app-specific control improvements and broader Phase 6 work remain pending. The
earlier 12 removed schedules have not been restored.
