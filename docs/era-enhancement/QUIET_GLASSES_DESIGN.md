# Hardware acceptance correction: quiet glasses and interruption

User reports repeated quiet-room misses and needing to shout over Era, despite
the CY03 glasses input working. Telegram works. Current settings: owner verifier
disabled, barge-in amplitude 0.04, echo factor 2, 700 ms post-speech hold.
Thus voiceprint mismatch is not established as the cause. Ordinary microphone
frames still traverse mute/state, attention and Gemini endpointing; latency can
come from several stages. Do not assert all misses have one confirmed cause.

Apply bounded Phase 4 acceptance fixes before advancing Phase 5:
- Use a lower 0.012 interruption floor only when the actual opened input name
  identifies CY03-CB36/Star Glass. Restore configured floor for other inputs.
  Retain VAD, sustained-speech and adaptive echo checks. No saved threshold edit.
- Retain four recent mic blocks during playback. On detected interruption, stop
  output and forward those blocks through the normal speaker/attention gates,
  checking mic/mute/standby/session ownership again on the loop thread. Avoid
  clipping the first command word. Clear history at mute/standby/quiet edges.
- Clarify Gemini's media instruction: low volume, headset distortion or short
  commands alone are not proof of media. Keep explicit echo/noise/media rules.
- Log actual input endpoint and rate-limited raw RMS/peak and capture disposition
  counts, without recordings or transcripts. These diagnostics distinguish quiet
  input from mute/echo/standby gates; do not bypass owner verification.

Tests use fake devices/PCM: quiet wearable interruption, PC floor unchanged,
echo/non-speech rejection, command prefix preserved once, late mute/session
change suppresses forwarding. A live normal-volume check is still required.
The hardware failure keeps Phase 4 partial. Phase 5 has not started.

Second live retest still missed quiet commands. Runtime logs confirmed the
actual CY03 input at 16 kHz, with several capture intervals peaking at only
0.0014–0.0032 RMS. These aggregate intervals are evidence of low-level input,
not proof every sample was user speech. Add bounded gain (up to 8x) for this
verified wearable input, with whole-frame peak limiting to avoid clipping.
Apply the same conditioned PCM to local interruption detection and ordinary
forwarding; keep diagnostics, standby and local-unmute audio raw. Restore unity
gain for other/unknown inputs; preserve all speaker, attention and echo gates.
This still needs a live normal-volume retest and false-trigger check.
