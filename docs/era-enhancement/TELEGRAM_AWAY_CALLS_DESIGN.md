# Location-gated automatic calls

User-authorized implementation, 2026-10-14: automatic calls only outside home;
iPhone location shared through Telegram. Computer inactivity cannot establish
absence. Compare explicit owner-shared coordinates with an explicit home anchor.

Alternatives considered: computer idle is unreliable; Wi-Fi phone absence can
mean a sleeping phone; Telegram live location supplies explicit geographic evidence
and reuses the authenticated owner bot channel. Implement the latter.

Home is inside 150 m including reported uncertainty; Away requires two updates
at least 30 seconds apart beyond 250 m plus uncertainty. Boundary/poor accuracy,
missing/expired updates or freshness older than five minutes yield Unknown.
Only Away permits proactive dialing. Home/Unknown retain normal reporting.
Location evidence is memory-only and clears on restart. Persist only the home
anchor and opt-in, encrypted with Windows DPAPI; no coordinate logging/history.

Owner-private-chat commands: /sethome arms the next fresh static location for
five minutes; /presence gives status and setup guidance; /autocalls on|off toggles
the opt-in; /forgethome removes the anchor. Reject forwarded locations, incorrect
sender/chat, out-of-order updates and expired live shares. Track edits of the
latest live stream, not arbitrary historic maps.

Eligible notifications: background completion/blockers, scheduled reports,
approval cards. Retain original message reporting. One attempt per identical
event, 15-minute cooldown, six attempts per local calendar day, no automatic
calls 22:00–08:00 Africa/Lagos. Busy calls are never replaced. A queued outgoing
call must recheck location after idle-listener cleanup. Caller greeting summarizes
the reason as notification data, never tool authority. No retry dialing when
unanswered; the existing message is the fallback. Incoming owner calls remain
independent of location and quiet hours.

Verification: deterministic GPS/expiry/accuracy/boundary/replay cases; owner
filtering and encrypted home persistence; no calls at home/unknown/quiet/busy;
duplicate/cooldown limits; location changes while queued; call-reason greeting
has no input authority. Live iPhone acceptance requires user setup and testing.
