# Operational reliability: acceptance and current defects

Later October 18 update: favorite identity/desktop playback, all-endpoint state,
source loading, speech-tail/capture and project verification advanced after
this historical audit. Read `STABILITY_RELEASE_2026-10-18.md` and the latest
handoff first. Earlier no-ID/restart-owed observations below retain their date;
they are not the current status. Physical acceptance still remains partial.

## Plan before implementation

Continue TODO #43 and audit prior work before adding features. Preserve the
purple HUD, existing long-term memory/vault, Claude subscription and protected
speech, approval, Spotify worker and media-routing paths.

1. Carry forward the October 16 live results with their date and limitations.
2. Read today's real runtime state; do not replay obsolete playback or output
   choices. The owner has now explicitly pinned Era voice to PC speakers.
3. Fix the reproducible endpoint-observer COM apartment mismatch. Era sets
   `sys.coinit_flags=0` before imports; the observer calls `CoInitialize()` (STA).
   Reuse an already initialized apartment only for RPC_E_CHANGED_MODE, balance
   only successful initialization, preserve registration ownership and surface
   all other failures. No audio streams or output routes are opened/changed.
4. Add failing regression cases first, then test borrowed/owned apartment cleanup
   and native MTA read/notification startup in a disposable process. Run the six
   protected suites and relevant awareness/routing regressions, scoped Ruff,
   compilation, manifest freshness and diff checks.
5. Record completed work and outstanding physical acceptance in handoff/TODO.

Additional live finding before its repair: the absolute-path shortcut in
`main.py` opens every path-bearing instruction (even rename/delete) and greedily
treats subsequent prose as part of the path. The disposable rename therefore
reported "Path not found" without reaching the approval gate. Restrict this
shortcut to a standalone explicit open/show request; keep bare paths as data
and route file mutations/compound instructions through the normal tool gate.
Add intent regressions for mutation, negation, compound requests, real paths
with spaces, and UNC paths. Preserve existing path extraction contracts.

## Acceptance evidence carried forward

On October 16 an authenticated normal HUD message asked the live model to call
`scheduled_task(action="pending")` and `audio_control(action="media_routing")`.
Both calls were observed in logs. The owner reported hearing:

> Nothing is waiting for your approval, sir, and Spotify is playing on the PC speakers.

This proves the combined read-only reply was audible and used those tools for
that turn. It does not prove the output device heard, the armed-confirmation
case, every speech turn, or resolution of a NEEDS APPROVAL row.

Earlier October 16 session history records routing Spotify to glasses and back
to speakers, with live speaker sessions afterwards. Successful favorite search
took 30,033 ms and retried a timed-out UI worker. Pause took 178 ms, resume 320 ms.
These are observations, not universal latency guarantees or new test actions.

On October 18 the live process is different, connected and in standby. Its
requested and observed voice output are PC speakers; media preference is also
PC speakers and no active media sessions were returned. Do not restore an older
glasses preference or wake the session unnecessarily.

## Implementation and verification

- COM repair: `core/device_awareness.py::_initialize_com` accepts only
  RPC_E_CHANGED_MODE as evidence of an already initialized usable apartment.
  Unrelated initialization errors still propagate. Reads and notification
  registration release only COM references they acquired; an unsuccessful
  unregister retains its ownership, as before.
- Three new borrowed-apartment regression cases failed before the fix. The
  complete device module now passes 25 cases. The native probe in the same
  MTA import mode as `main.py` read four endpoints, published ready native
  notification coverage, stopped cleanly and released registration. This is
  registration/initial-state proof, not a real hardware unplug/reconnect test.
- Absolute-path repair: `app/text_intents.py::_is_path_open_request` and the
  one caller in `main.py` reserve direct opening for standalone open/show
  orders. Renames, deletes, overwrites, negations, discussions and compound
  orders continue through ordinary routing and approval. Fifteen intent cases
  cover these distinctions, including paths with spaces and UNC paths.
- AST comparison against the pre-edit backup confirms that unrelated `main.py`
  logic is unchanged (only the helper import and shortcut condition differ),
  and all existing text helper bodies remain unchanged. Ruff formatting caused
  no additional semantic change. Prompt BOM remains intact.
- Current final operational suite: **425 passed, 2 warnings**. Separate
  enhanced-directive/personal-control/vision/memory suite: **217 passed,
  2 warnings**. Fresh manifest suite: **8 passed**. These are 650 selected
  tests across three runs, not a full-suite claim. The required speak_once,
  media routing, Spotify search, HUD approval, scheduler authority and toolspec
  tests are included in the 425-test run.
- Scoped Ruff check/format and Python compilation pass. Manifest regenerated
  for current source: 402 modules, 59 tools, zero unparseable modules. No tool
  declaration or golden file was edited. Protected speech, approval-store/HUD,
  Spotify-worker and media-routing implementations remain intact. The two
  previously documented audio/crew failures were not weakened or claimed fixed.

Evidence: `runtime/operational-final-regressions-20261018.log`,
`operational-backlog-regressions-20261018.log`,
`operational-manifest-tests-20261018.log`,
`operational-native-device-check-20261018.log` and
`operational-semantic-check-20261018.json`. Pre-edit source backup:
`.phase7_backups/operational-reliability-20261018/`.

## October 18 armed-approval test: precise outcome

A newly created disposable file was the only mutation target. The first command
was ignored in standby; after normal HUD wake, an absolute-path command hit the
broken opener. A relative-path request then reached the normal confirmation
gate. The tool arguments contained the desired destination in `name`, omitting
the required `new_name`. No application approval was synthesized by this agent.

The live `scheduled_task(action="pending")` named the armed file action. The
15-second Action Required card's display timeout is intentional; the Live Tasks
row remained `awaiting`. Do not confuse a hidden card with an empty store.

The recorded voice transcript contains **"Yes, proceed."** before the later
**"Cancel cancel."** The gate accepted the earlier approval, cleared the pending
row, attempted execution, and reported `Not found` because of the incorrect
rename arguments. The owner subsequently reported "Cancelled; NEEDS APPROVAL
cleared". Both observations are retained: the badge visibly cleared, but this
is **not a clean cancellation/no-execution proof**. It proves armed-pending
lookup and approved-resolution cleanup; it does not prove successful rename.

The original disposable file was unchanged and the destination absent. Only
that exact unchanged fixture was removed. The original standby state was
requested again after the test; current voice/media device choices were not
changed. Speech transcripts also contain fragments resembling Era's preceding
speaker output; possible acoustic pickup requires hardware validation, not an
unrequested voice/media filter or a change to the owner's output choice.

The standby restoration completed. Subsequently the owner woke Era and asked
for voice on glasses: `audio_control(set_voice_output, device="glasses")` at
11:53:11 UTC, followed by matching opened-stream evidence at 11:53:12. Final
API readback shows Star glass for Era voice and PC speakers for media. This
newer owner choice was left intact; the active session was not forced back into
standby again.

## Backlog audit: preserve existing work

| Original area | Existing implementation / evidence | Remaining acceptance or work |
|---|---|---|
| CodeMode intent, model persistence, subscription isolation | `core/codemode_intent.py`, `codemode_models.py`, CLI/runtime/workbench; enhanced-directive regressions | Retain subscription-only billing rule; live voice/model/session continuity is not universally proven |
| Audio crash protection | `core/audio_pcm.py`, live audio guards; one-byte/frame regressions | Known separate audio-reclaim test debt; hardware first-attempt/echo checks remain |
| Device awareness | This pass fixes native COM startup/read/notifications | Restart Era to load the patch; real hotplug/reconnect acceptance remains |
| Spotify speed and exact IDs | `core/spotify_api.py`, `core/personal_media.py`, killable UI worker; current tests | Favorite exists but has **no saved Spotify URI/ID**. Name-search retries took 30 s; obtain a verified identity before saving it, never invent one |
| Spotify contexts | Protected named Play controls and Release Radar sidebar path | Liked Songs/Release Radar physical playback still owed; no new playback initiated here |
| Media routing | `core/media_routing.py`, `audio_control(route_media)`, actual session readback | October 16 switch/readback evidence exists; no current-media playback means no new active-route proof |
| Approvals and speech | Required suites pass; live combined read-only reply heard; armed action named | Exact rename arguments and a clean cancelled-action test remain. Preserve existing authority and `speak_once` |
| Crew/Laura and tool recovery | `crew/reliability.py`, `core/tool_health.py`; enhanced-directive fault checks | Live hung-agent recovery/fallback not claimed; known whole-call budget failure remains separate |
| Reconnect/context | `app/input_buffer.py`, persistent bounded context; replay/consent regressions | Real network interruption and context-restoration acceptance remains |
| Memory | Existing memory manager, `memory/memory_context.py`, feedback, long-term store and vault | No replacement store or cloud backup; broader preference-learning accuracy needs real-use evaluation |
| Vision | Existing service/worker/cache/input validation; current silent/worker/async regressions pass | Real provider/sample-image accuracy and capture latency still need measured acceptance; no wholesale rewrite justified yet |
| Background reports | Existing task outcomes/queue/reporting from prior phases | Live success/failure notifications and room audibility remain user acceptance |
| HUD views, files, layout | Registered Telegram view, retained recent files, existing open/reveal actions; prior HUD checks | Visual polish is paused by owner; real generated-file open/reveal acceptance remains |
| Netflix next episode | Existing player state/resume fixes and prior Protocol One repair evidence | Physical episode transition not retested this pass |
| Test-suite timeout | Existing bounded runner, external-call isolation and prior full-run reports | This selected regression run does not close the current full-suite/known-failure debt |

## Next steps

1. Restart Era to load the two verified source fixes. The current process still
   uses its old imported code; source/native-probe success is not claimed as
   a live-process repair before restart.
2. For Spotify's speed pass, resolve the verified favorite's real share URI/ID
   and persist it through existing memory. Preserve the working title fallback,
   killable worker, actual-track verification and single retry.
3. Finish the remaining #43 acceptance with exact tool arguments and separate
   approval/cancellation observations. Then measure real vision accuracy and
   memory use against existing implementations, rather than building duplicates.
