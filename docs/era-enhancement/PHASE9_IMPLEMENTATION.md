# Phase 9 implementation checklist

The user approved CODEMODE_DESIGN.md with “proceed” on 2026-10-15.
Preserve the accepted HUD and all existing workspace edits.

Completion: items 1–7 are implemented and software-verified, including the real
read-only subscription/session probe. Physical Era voice/Telegram and a reviewed
live-patch walkthrough remain in PHASE9_PROGRESS.md.

1. Implement explicit mode/session state, source identity, revision/event ownership
   and cancellation; persist redacted metadata, never authentication material.
2. Implement a pinned subscription-only CLI session with bounded streaming,
   disabled tools/integrations, process-tree cleanup and explicit failures.
3. Assemble bounded current source context with hashes and checkout identity.
4. Route typed, Telegram and transcribed voice inputs through the same mode boundary;
   suppress parallel live-model output and reuse SpeechOut/captions.
5. Submit candidates through existing Development, adding cancellable verification,
   revision-bound expiring approval and exact cross-channel decisions.
6. Add mode/status controls inside existing HUD surfaces and register the public
   capability/prompt. Run focused offline regressions, frontend checks and Doctor.
7. Perform read-only CLI authentication/stream acceptance when locally available;
   clearly record hardware/live-promotion acceptance that cannot be observed here.

CodeMode is off at startup and enters only on an explicit user action. No automatic
live edit or generated-change promotion is authorized by entering CodeMode.
