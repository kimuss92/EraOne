# Telegram call input repair — 2026-10-14

## Observed failure and verified cause

The owner heard Era's greeting, spoke, and received no answer. The latest real
generation recorded native connection, Gemini greeting audio and native output
submission, then remote hangup. Neither native_input_received nor
gemini_input_forwarded appeared. The failure therefore preceded Gemini input.

The bridge incorrectly configured the PLAYBACK speaker field and accepted only
INCOMING/SPEAKER callbacks. The installed PyTgCalls 3.0.0 RecordStream(audio=True)
configures the microphone field for incoming participant audio. Correcting both
the stream description and callback selector resolves that routing mismatch.
Incoming audio is mono int16 at 16 kHz; outgoing audio remains mono 24 kHz, with
the previously repaired native 10 ms output submissions unchanged.

The previous fake SDK and description probe checked an internally consistent but
incorrect speaker mapping. The real SDK probe now compares the bridge against
RecordStream(audio=True, audio_parameters=AudioParameters(16000, 1)), rather than
only checking that the bridge's own descriptions convert without errors. This is
a lesson for future SDK integration tests: compare semantic routing with the SDK's
canonical constructors, not merely schema acceptance.

## Conversation policy

The verified owner-only Telegram stream no longer uses the PC/glasses voiceprint
gate. Phone microphone characteristics and speech level cannot silently block the
call. Session lifetime and PCM validation remain enforced. Native peer/generation
checks, immutable completed transcripts, tool ownership, duplicate prevention and
authenticated action approvals remain enforced. This changes no local microphone
or local speaker-recognition settings. Account-bound remote input does not claim
to be biometric verification of the person holding the phone.

The call prompt requests a warm, short greeting instead of "system online", natural
replies to intelligible speech and follow-ups without a wake word, and interruptions.

## Verification and pending acceptance

The installed optional SDK probe passed against its real RecordStream constructor,
without networking, calls or physical audio devices. Regressions check remote
microphone acceptance, outgoing audio and speaker mix exclusion, quiet phone PCM
passthrough without constructing a voiceprint gate, invalid PCM rejection and
session closure. Existing owner/approval/transport tests remain in the regression run.
Final targeted run: 192 passed; strict mypy on the changed runtime/helper/probe
and scoped Ruff passed. The existing google-genai deprecation warning remains.

The first expanded test run exposed a two-second wall-clock timeout in the existing
six-second audio-burst regression on this busy Windows host: 191 tests passed and
that wait timed out. The wait now allows eight seconds; production timeout and
buffer limits were not relaxed for this test adjustment.

An attended call after restarting Era must still verify first-attempt recognition,
quiet speech, follow-up turns, interruption and hangup. No real call was placed by
the agent during this repair. Clear output was confirmed by the owner before this
input repair; successful incoming recognition is not yet marked verified.
