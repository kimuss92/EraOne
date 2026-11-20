# Glasses microphone diagnosis — 2026-10-12

## Confirmed cause

Windows and the Python app were configured for `Casque (CY03-CB36)`, but Era's
independent `config/voice.json` preference `voice_input` was `Microphone Array`.
`core/voice_input.py` resolves that preference to an explicit PortAudio input;
`app/era_live/audio.py` passes it to `sd.InputStream(device=...)`. This overrides
the default input. Recent logs repeatedly reported `Microphone Array` (device 2),
including 16:37:54. This explains why proximity to the computer helped.

## Correction and verification

- Saved `voice_input` as `Casque (CY03-CB36)` using the existing preference API.
- Verified that all other voice configuration keys were preserved.
- Fresh-process resolution selected the glasses' MME input, device 1. Numeric
  indices are process-specific and are not persisted.
- PortAudio's format check passed for mono, int16, 16,000 Hz on the glasses'
  MME and DirectSound inputs. WASAPI rejected this rate; it was not selected.
- No microphone recording, Bluetooth reset, output-volume change, or running
  task interruption was performed.

The existing open capture stream does not watch preference changes. Restart Era
to apply this preference reliably; a new capture session also reads it again.
The user subsequently confirmed the correction works ("perfect it work"). This
provides user acceptance of the selected glasses input, rather than a recorded
acoustic benchmark. This
correction does not establish the cause of earlier physical Bluetooth disconnects.

## Lesson

Windows' default microphone and Era's explicitly selected microphone are separate
settings. Diagnose the saved preference and actual capture logs together. Future
input observability should report the open stream's actual device and distinguish
it from a newly saved preference. Do not silently substitute the laptop microphone
when the user explicitly requests the glasses.
