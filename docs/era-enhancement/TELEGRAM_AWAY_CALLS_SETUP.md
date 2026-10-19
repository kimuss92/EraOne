# iPhone setup: automatic calls only while away

Implemented 2026-10-14. Restart Era once to load the new bot location handler and
call policy. Incoming calls remain available independent of home presence.

1. iPhone Settings → Privacy & Security → Location Services → Telegram: enable
   Precise Location and Always access for background sharing. If Always is not
   initially offered, start live sharing and accept Telegram's background prompt,
   then revisit settings. Keep location services enabled.
2. While physically at home, open your existing private **Era bot chat** used for
   text commands and approval buttons. This setup uses that bot, not the separate
   Era account used for native phone calls. Send `/sethome`.
3. Within five minutes, attachment (+/paperclip) → Location → **Send My Current
   Location**. Wait for Era's confirmation that home was saved securely. This
   enables location-gated automatic calls; it does not immediately call you.
4. In the same bot chat, attachment → Location → **Share My Live Location**.
   Choose a duration covering your outing. Renew it when it expires. Choose a
   longer/indefinite duration only if your current app offers it.
5. Send `/presence` to inspect Home, Away or Unknown, and the automatic-call switch.
   Initially outside is Unknown until a second distinct update confirms the
   departure after at least 30 seconds. Location more than five minutes old cannot
   authorize calls. iOS/background/network interruptions can therefore pause
   proactive calls until fresh updates arrive.

Commands: `/autocalls off`, `/autocalls on`, `/forgethome`.
The last removes the locally encrypted home point and turns automatic calls off.
There is no stored travel history, location is never logged, and restart forgets
current phone evidence. Only the verified owner private chat may establish home;
the calling account independently checks the same owner identity.

## Notification behavior

Background CM/crew reports, explicitly queued agent-task completion/blockers,
scheduled-task reports and new Telegram approval cards may request calls. A
separate urgent event category exists in the policy; general OS/GodEye alerts are
not yet connected to it. Routine PC replies and telemetry do not trigger calls.

Calling requires accurate live location beyond 250 m from home, with uncertainty
accounted for, two stable updates at least 30 seconds apart, a configured calling
service with no busy call/cleanup, and the away-call switch on. Inside 150 m is
Home; the boundary band is Unknown. Computer inactivity never establishes Away.

No proactive calls 22:00–08:00 Africa/Lagos; a 15-minute cooldown, a six-attempt
daily limit and event deduplication persist across restarts. No unanswered-call
redial loop. Existing Telegram reporting remains; eligible calls also mirror the
reason in text as fallback. On answer, Era explains the reason and allows normal
conversation; notification data supplies no tool/approval authority. Existing
approval buttons are still required for protected actions.

Presence is checked both when requesting a call and immediately before native
dialing, including when replacing an idle incoming listener. If you return home
during preparation, dialing is cancelled and incoming listening resumes.

## Verification

The first broader run found an optional hook incorrectly assuming every report
sender had a live event loop. It was fixed to preserve existing reporting when
the loop is unavailable/closing. A new test also had an incorrect mock response
shape; its assertion was corrected. A strict typing finding from a reused local
variable and a scoped import-format finding were corrected.

Final regression result is recorded below. Tests use synthetic locations and
fake native transports: no real call or Telegram message was sent by the agent.
Home/live-sharing configuration and real iPhone background updates remain owner
acceptance steps. Full project-wide checks were not rerun for this change.

Sources:
- https://core.telegram.org/api/live-location
- https://core.telegram.org/bots/api#location
- https://translations.telegram.org/en/ios/general/AccessDenied.LocationAlwaysDenied

Final verification: 511 targeted regressions passed in 22.17 seconds. Scoped
Ruff/Black, strict mypy for the four affected call/presence modules, and compilation
of all affected production modules passed. Existing Google SDK emits one pytest
deprecation warning. No home coordinates have been configured by the coding agent;
automatic calls remain blocked until the owner completes the setup above.
