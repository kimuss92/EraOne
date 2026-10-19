# Voice responsiveness restoration — 2026-10-13

Status: software fix verified in focused checks; physical acceptance pending.
Later phases remain on hold.

## Evidence

The latest available log repeatedly records `attention.ignored` from
2026-10-12T23:49:19Z onward, with empty or unknown Vosk name recognition.
That gate replaces user audio with silence before it reaches Gemini; text input
does not traverse it. The saved attention window was 120 seconds and expires
while other applications play sound. This explains a concrete voice/text
asymmetry, but not every possible missed utterance.

Git history dates the listening system and media/name gate changes to
October 10, with a first-sentence correction on October 11. This matches the
user's reported regression period; runtime audio drops supply the direct
evidence rather than dates alone.

The same log shows an `enter_standby` tool call at 23:59:25Z, followed by
standby microphone routing until a verified wake at 2026-10-13T00:04:27Z.
There is no evidence here establishing that the standby command was accidental.
Normal conversation receives gain-limited wearable amplification, but the
standby branch discarded that conditioned frame and fed raw microphone audio
to its local recognizer. That is a confirmed second mismatch.

## Repair and trade-off

Use the already-supported `listening.attention_window_s = 0` preference to
disable the timed name requirement for this user's active conversations. This
is deliberately broader than merely extending the timeout again: active Era
must receive ordinary requests regardless of how long the user paused or
whether media is playing. The optional timed mode and its tests are retained.
The model is explicitly instructed not to require repeated names or treat
inactivity as a reason to ignore an ordinary request.

Keep acoustic echo hold, barge-in echo rejection, speaker verification policy,
microphone-off and explicit standby controls. The local standby recognizer now
receives the same confirmed wearable gain as the active voice path, without
sending standby audio to the cloud or changing its confidence requirements.
Unamplified PC input remains unchanged.

Disabling the name gate means background speech can reach the speech model,
which still rejects clearly identified playback or speech meant for someone
else. Perfect intent or background discrimination is not claimed. Replacing
the recognition engine or lowering security thresholds is unnecessary for
these verified defects. The logs do not prove an upstream recognition fault
is eliminated, and network/recognition/speech-generation latency still exists.

## Verification

Focused tests: 114 passed, one Google SDK deprecation warning, no failures.
Artifact: `listening-restoration-checks.txt`.
New regressions cover the first unnamed request after one hour with media
active, retained echo protection, standby wearable gain, unchanged PC gain,
mic-off privacy, and keeping standby audio off the cloud queue.
Scoped Ruff checks passed. Existing speech regressions are rerun separately.
Registered speech pipeline result: 425 passed, SDK deprecation warning only;
artifact `listening-restoration-speech-checks.txt`. Manifest regeneration and
currency check passed; `git diff --check` passed. No test failures occurred in
this repair. One documentation patch had an incorrect context and was retried
successfully without altering production code.
No microphone recording, external message, driver modification, or live
process restart was performed.

## Hardware acceptance

Restart Era, make sure she is awake and the microphone is enabled, then ask
normal-volume questions without saying Era before every request. Repeat after
more than two minutes, and while music is playing. Interrupt a reply normally.
After explicitly requesting standby, test the configured wake word at normal
volume. Watch for self-responses; they remain an acceptance criterion.
Do not advance phases until the user confirms voice responsiveness.

## Subsequent runtime evidence

Latest observed restart: 2026-10-13T01:07:38Z. The following connection opened
Casque (CY03-CB36), input index 1, 16 kHz, gain 8, with glasses output index 5
and no output fallback. Voice inputs were accepted at 01:07:59Z and 01:08:07Z;
both were followed by reply/playback latency events. This establishes working
voice requests after restart, not first-attempt acoustic reliability or absence
of self-triggering. Asked the user for those acceptance results. A full global
readiness run completed successfully: all 31 gates passed in 466.7 seconds,
`GLOBAL_READY: YES`, `ALL_CHECKS_OK`. Artifact:
`listening-restoration-global-checks.txt`. Hardware acceptance remains pending;
these passing gates do not establish that every spoken attempt is recognized.
