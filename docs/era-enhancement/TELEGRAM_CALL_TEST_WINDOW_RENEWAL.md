# Disabled-call control repair — 2026-10-14

The disabled controls were caused by an expired 30-minute attended test window.
Read-only inspection confirmed authenticated/account_authenticated/owner_bound
were all true, calls_enabled=true, attended_test_mode=true, and the deadline had
elapsed. Daily-use duplex validation has not yet been marked complete.

Renewed the protected test window with the existing explicit activation helper.
No call was placed. Added Enable calls for 30 minutes to the authenticated HUD for
configured, disabled call services. The runtime rechecks the account's independent
owner binding and original setup validations before saving an opt-in. It then
updates the current service and expiry timer, rather than recreating/restarting Era.

Renewal does not place a call, connect to Telegram or prompt for credentials.
It is unavailable while a call or unresolved cleanup is active. Requests are tied
to the current generation. A service-local async lock makes double clicks
idempotent without duplicate saves or repeated deadline extension. Persistence,
owner/authentication or stale-generation failure leaves calling disabled.

The HUD now explains that the test window has expired instead of claiming that
the whole integration remains unavailable. The new Python runtime code needs
one initial restart to install it; subsequent renewals use the HUD button with
no restart. Existing in-memory code cannot acquire this new handler by refreshing
the webpage alone. Research of the existing reload mechanisms found supervised
full-process reload, rather than a supported subsystem hot-reload path.

Verification covers successful renewal after expiry, a second renewal in the same
service, concurrent double clicks, stale generation, changed owner, failed auth
and failed settings persistence. No permanent call activation or proactive calling
was enabled. Attended two-call/music/hangup acceptance remains pending.

Verification: 210 targeted call tests passed; TypeScript compilation and HUD
production build passed. Scoped Ruff/Black, strict mypy on the two changed runtime
modules and compilation passed. One existing google-genai deprecation warning
remains in pytest. The full global suite was not rerun.
