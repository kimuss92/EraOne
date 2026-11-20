# Era Workbench and Connect

The Brahma-inspired expansion uses Era's existing voice session, tools, approvals,
development pipeline and HUD. Spotify MCP is excluded. The source comparison is
in [the integration inventory](BRAHMA_INTEGRATION_2026-10-20.md).

## Start here

After restarting Era, open **Settings → Capabilities & Connections**.

- **Desktop launcher:** show the draggable floating E icon. Click it to open Era;
  right-click for workspace, startup, visibility, restart and quit controls.
  Restart/quit ask for confirmation. Hiding the icon leaves its tray menu available.
- **Services, vision & document folder:** configure Gemini voice/vision, Ollama,
  Home Assistant, the document folder and REA CLI path. Save each changed field.
  Existing secrets are masked; entering and saving a blank secret clears it.
  Windows credentials use Era's existing DPAPI protection. Connection tests
  contact the selected service; they do not purchase or generate content.
- **Era Connect:** start on your PC's private network address, then pair a phone.
  Scan the QR code within two minutes, name the device and select Pair device.
  Use Safari → Share → Add to Home Screen on iPhone. Both devices must reach the
  same trusted network. The current transport is HTTP, so traffic is unencrypted.
  Desktop approval controls are never exposed through this companion.
  Stop the listener or revoke individual devices from Settings.
- **Skill Crucible:** describe a reusable skill and review its staging job in
  Development. Generated code must pass checks and receive final approval before
  activation. Existing approved skills remain managed by Era's feature registry.

## Requests to try

Use the usual Era command prefix and describe the desired result:

| Capability | Example |
| --- | --- |
| Documents | “Create a Word project brief with objectives, milestones and a risks table.” |
| Spreadsheets | “Create an Excel budget with a totals formula and a category bar chart.” |
| Presentations | “Create a five-slide project update with speaker notes.” |
| PDF | “Merge these two PDFs into a new file” or “Extract pages 2 and 3 from this PDF.” |
| Desktop | “List the processes using the most memory” or “Inspect the controls in this window.” |
| Clipboard | “Read my clipboard and summarize it.” Reading is explicit, not continuous monitoring. |
| Skills | “Forge a skill named invoice_totals that validates and totals a CSV invoice.” |
| Reverse engineering | “Inspect this source folder” or “Use REA to analyze this Electron application.” |
| Native analysis | “Decompile the entry procedure in this executable using Ghidra.” |
| Vision | “Look at my screen.” Existing screen/camera tools and provider fallback remain authoritative. |
| Geography | “Show this latitude and longitude on a map” or “Get the latest USGS earthquakes.” |
| Smart home | “List Home Assistant devices.” Changes require the existing confirmation flow. |
| Circuits | “Give me DHT11 wiring and sample firmware for an ESP32.” |

Documents default to `runtime/deliverables`. The tool returns the actual saved
path and parses the file again before reporting success. Existing files are not
overwritten. Excel evaluates formulas when the workbook opens; generated layouts
still need review in the intended Office application.

## Installation and maintenance

`tools/install_era_capabilities.ps1` installs the added Python packages, pinned
REA 6.7.0, and checksum-verified portable Ghidra 12.1.4, JDK 21 and WABT 1.0.42.
The portable tools live under `%USERPROFILE%\.era\tools`; no global Java or PATH
change is required. The existing Era requirements provide Word, PowerPoint,
PyQt and Windows automation dependencies.

REA supports static JavaScript/Electron, managed-artifact inspection, WebAssembly,
artifact metadata and native decompilation. Full evidence is retained in the
document output folder. Native decompilation uses Ghidra explicitly with a
three-minute ceiling; other analyses have a 90-second ceiling. Cancellation
terminates the owned analyzer process tree. Larger targets may exceed these
bounds and return failure rather than a fabricated result. Inspected programs
are not launched by these tools; pseudocode is not original source.

## Boundaries and remaining acceptance

This is not complete parity with every Brahma feature. Connect is a mobile web
companion, not a native iOS app: no cellular/SMS interception or background GPS.
Physical Safari pairing and real Home Assistant devices require live acceptance.
The circuit tool currently covers DHT11 with Arduino Uno or ESP32, with no
electrical testing or firmware flashing. Maps provide links and great-circle
geometry, not a 3D globe or driving navigation. No Discord, fitness or gaming
account integrations were added. Existing Era social/browser/Telegram systems
remain in place. Skill staging uses subprocess tests, not an OS sandbox.

Validation evidence and remaining work are tracked in `handoff.md` and `TODO.md`.
