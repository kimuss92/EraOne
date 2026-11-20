# Approved Telegram setup and restart design

User approved this design in chat on November 4, 2026.

Add a guided local HUD dialog with account details, application credentials help, code/password entry, owner verification, and explicit enable action. Use the existing isolated Telegram Python environment via private stdin/stdout, never command arguments. Reuse current identity checks and configured owner ID. Keep a separate Windows-protected draft; preserve the working account until successful activation, backing up replaced credentials. Never echo credentials or return them through status APIs. Resume authenticated drafts and expose controlled actionable errors. Activation requires a restart to load settings and provides the existing restart action.

Recognize whole-utterance restart/reboot requests addressed to Era, including polite forms, while excluding negation, questions about past events, and PC restart commands. Check relaunch scheduling before acknowledging success. Keep busy-job confirmation. Use restarting wording before exit, online wording only on reconnect.

Implementation: isolated setup worker, authenticated HUD endpoint, guided React dialog; command matcher and restart failure ordering. Verify fake-client authentication transitions, identity guards, API behavior, command negative cases, existing lifecycle/setup suites, frontend build and type check. Production Telegram is already user-confirmed working; do not disturb it for automated tests. First-request voice acceptance remains a user live check.
