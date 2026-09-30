# Era — Community-Management Command Reference

What to **say or type** to Era to run the content workflow for a brand, from idea
to published post. Voice and typing both work; typed orders skip the mic entirely
and are the most reliable for exact wording (contact names, prices, links).

> **How Era decides it's CM work:** an order that names a brand **and** a CM verb
> (plan / prepare / produce / make a reel / publish / post / schedule a post) is
> routed to the community-management lane. If the brand is unclear or you name
> two, Era asks **"Which brand is this for?"** — just reply with the brand name.
>
> **Runs as a background agent.** You no longer have to type CM orders — **say
> them** and Era hands the job to the community-management agent, which runs it in
> the background. Era stays free to take other requests while it works, and speaks
> the result back to you the moment the job finishes (success, "wrote 0 files"
> caveat, or failure). Typing still works too and is best for exact wording.
>
> **One job at a time.** All brands share one Magnific/Adobe login, so jobs run
> strictly one after another. Ask **"what's running right now?"** anytime.

---

## Brands & where posts go

| Brand | Say any of | Posts to |
|-------|-----------|----------|
| **Delta Cuisine** | "Delta Cuisine", "delta", "DC" | Instagram @delta_cuisine_group + Facebook (Composio) |
| **Ceram Quartz** | "Ceram Quartz", "ceram", "quartz", "ceramquartz" | Instagram @ceramquartz + Facebook "CQ" (Composio) |

Era always states the **Brand Lock** (exact accounts) before producing or
publishing, so a post can never land on the wrong page.

---

## 1. Plan the week

Era writes the **complete** week: every reel and carousel, its slot, its hook, the
**material it chose** (family + exact swatch, with the reason), and the caption.
Then it stops and shows you the plan.

- "**Plan Delta Cuisine's week.**"
- "**Prepare this week's content for Ceram Quartz.**"
- "**Build a promo campaign for Delta Cuisine.**"

**Era decides the creative work itself.** It never asks "quartz or ceramic?" or
"which texture?" — it picks from the brand's swatch library, obeys the harmony laws
and the wardrobe conception references, and tells you what it chose and why. You
approve what's right and correct what isn't.

## 2. Approve the plan  (Gate 1)

- "**Approve the plan.**" — all items go to production.
- "**Approve reels 2 and 3, change reel 1.**" — the rest wait for your correction.
- Or edit `plan.md` yourself: set each item's `status:` to `plan_approved`.

Approved items are produced **one at a time**. Each finished asset is saved as a
**draft** and shown to you — it is *not* published yet.

## 3. Check the production, then approve the asset  (Gate 2)

**Approving the plan is not approving the reel.** A render can come out wrong even
from a plan you liked, so Era shows you every finished asset — file path, the
material it used, the caption — and emails it to the brand's notify address so you
can see it when you're away from the machine. Then:

- "**Approve reel 1.**" — Era marks it `(approved)` and schedules/publishes it.
- "**Change reel 1's texture.**" — Era regenerates it and shows you again.

Era **never** writes `(approved)` on its own. That string is your signature, and
nothing is published without it.

## 4. Correct a specific item

Name the item and the change. Era revises **only** that item — it does not re-plan
the week. If the item is still just a plan entry, the plan changes; if it was already
produced, the draft is regenerated and comes back to you for Gate 2.

- "**Change reel 1's texture.**"
- "**Rewrite the description of carousel 2.**"
- "**Redo the wardrobe reel with a darker material.**"

## 5. Quick creation (sudden campaign — always available)

Works any time, even while the weekly plan is mid-production.

- "**Urgent: make a promo reel for Delta Cuisine right now.**"
- "**Quick campaign for Ceram Quartz, asap.**"

Era decides everything, produces it, and shows you. It won't publish a rush asset
until you say so.

## 6. Publish now  (force-post approved assets immediately)

Approved plan items schedule themselves for their slot. Use this only when you want
them out **right now**, ahead of schedule.

- "**Publish Ceram Quartz's approved posts now.**"
- "**Quick publish the Perla reel in Ceram Quartz.**"
- "**Post it now.**" / "**Publish Delta Cuisine.**"

What Era does: finds every `(approved)` file with no `.posted` marker, writes/keeps
its caption (brand voice + the correct FR **or** AR footer), **hosts the video to a
public URL automatically**, posts to that brand's Instagram + Facebook with the
exact account IDs, writes a `.posted` marker so it can't double-post, then emails
the brand's notify address a one-line report.

> **Large videos are handled now.** IG/FB can only fetch a video from a public URL,
> so Era uploads the local reel first (via `media_host.py`) and posts that URL.
> This is what previously blocked the 66 MB Perla reel. To use your own CDN instead
> of the default host, set Cloudinary or Bunny creds (see `media_host.py` header).

---

## 7. Scheduling (recurring, automatic)

Each brand has two recurring jobs — **weekly_plan** and **auto_publish** — read
from `brands.yaml` (times are Africa/Tunis). Era runs them on their own; you can
inspect and change them by voice/text:

- "**What's scheduled?**" → lists every brand's weekly-plan and auto-publish time + on/off.
- "**Move Delta Cuisine's weekly plan to Thursday 10 a.m.**"
- "**Turn on Ceram Quartz auto-publish.**" / "**Disable Delta Cuisine auto-publish.**"
- "**Change Ceram Quartz auto-publish to Tuesday 8 p.m.**"

Current state (`brands.yaml`, verified 2026-07-09) — **all four are `enabled: false`**,
so nothing fires on its own yet:

| Brand | Weekly plan | Auto-publish |
|-------|-------------|--------------|
| Delta Cuisine | Fri 11:00 — off | Fri 21:00 — off |
| Ceram Quartz | Tue 11:00 — off | Tue 20:00 — off |

> Turn a schedule on by voice (*"turn on Delta Cuisine's weekly plan"*) or by setting
> `enabled: true` in that brand's `schedules:` block. Until then, run the pipeline on
> demand: *"Era, plan Delta Cuisine's week."*

---

## 8. Promotions (start / end a discount)

Era keeps each brand's live offers in `brands.yaml`. An **active** offer is used in
promo content without ever asking you whether it's still running — so when a promo
ends, tell Era once:

- "**Era, the -10% discount is no longer valid.**"
- "**Stop using the -10% for Delta Cuisine.**" / "**The -10% is over.**"
- "**Reactivate the -10% for Delta Cuisine.**"
- "**What promos are running?**"

Era flips that offer's `active` flag immediately. From that moment the community
manager never sees it again, so it cannot leak into a new caption or poster.
Already-published posts are untouched.

> Currently live: **Delta Cuisine — -10%**. Ceram Quartz has no offer configured.

## 8b. Meta Ads (paid campaigns)  *(added 2026-07-20)*

Separate from everything above — a paid campaign runs under its own account,
its own rules, and its own gate. Say things like:

- "**Boost this post.**"
- "**Run a Meta ad campaign for the Rosières pack.**"
- "**Sponsor this reel.**"
- "**What's the ad budget for Ceram Quartz?**"

**Era will ask you two things for every single campaign, no exceptions:**
when it should end, and what the budget is. There is no standing budget you
set once — a fresh campaign always gets a fresh answer, even if it's similar
to a previous one. Given either a daily figure or a total, Era computes the
other side and states the total spend before creating anything.

**Every campaign is created PAUSED.** Era shows you a review summary and
stops — nothing goes live until you say so. This is the same two-gate
discipline as organic content, just with money attached, so it gets its own
explicit stop.

- "**Approve the ad campaign.**" / "**Activate it.**" — turns it on.
- "**What Meta ad accounts do we have?**" — lists them.

Runs under the freelance account (where the ad connection lives), never a
brand's own account — you won't notice this, but it's why an ads order can
briefly feel slower to kick off than an organic one.

## 9. Chatting with Era from anywhere (Telegram)

Era listens on Telegram by **long-polling** — her laptop calls out to Telegram and
waits. That means it works from **any network**, with no port forwarding, no static
IP and no tunnel. She only has to be running at home.

**You can:**
- Send an order from anywhere: *"plan Delta Cuisine's week"*, *"the -10% is over"*.
- **Hold the mic button and just say it** — a voice note works exactly like typing.
- Receive every finished draft as a video/photo with its caption, for Gate-2 review.
- Reply *"approve reel 1"* or *"change reel 1's texture"* right in the chat.
- Get the CM job's result pushed to you when it finishes (they can take 30 minutes).

**Setup**

1. In **@BotFather**: `/newbot` → give it a name → copy the token. Use a bot dedicated
   to Era; don't reuse one that another service already talks to (see the webhook
   warning below).
2. **Open your new bot in Telegram and press Start.** A bot cannot message a user who
   has never started a chat with it — skip this and Era's first push fails with 403.
3. Set the environment variables (PowerShell), then **restart Era** — `setx` only
   affects processes started afterwards:

   ```powershell
   setx TELEGRAM_BOT_TOKEN "<token from BotFather>"
   setx TELEGRAM_CHAT_ID   "1704700117"
   setx ERA_REMOTE_TOKEN   "<a long random string>"
   ```

4. Check it before restarting:

   ```bash
   python era_telegram.py doctor        # names the bot, warns about webhooks
   python era_telegram.py send "hello"  # should arrive in your chat
   ```

| Variable | What it is |
|----------|-----------|
| `TELEGRAM_BOT_TOKEN` | Your bot's token from @BotFather |
| `TELEGRAM_CHAT_ID` | **Your** numeric chat id — the only sender Era will obey. Yours is `1704700117` |
| `ERA_REMOTE_TOKEN` | Shared secret for the LAN `/remote_voice` endpoint (below) |

Without the first two the Telegram bridge stays dormant and everything else runs
unchanged.

> **A bot can use a webhook *or* long-polling — never both.** If anything has
> registered a webhook on your bot, Telegram delivers your messages *there* and Era
> receives nothing (every poll returns `409`). `era_telegram.py doctor` detects this
> and prints the offending URL. A webhook **you did not set** means someone else has
> your token: revoke it in @BotFather immediately.

> **Security.** Anyone can find a bot and message it, and Era acts on your real
> machine. So every message whose `chat.id` is not exactly `TELEGRAM_CHAT_ID` is
> dropped, silently and with no reply. Never share the bot token.

### Voice notes

Hold the mic button in Telegram and speak. Era transcribes the note, echoes back
**`Heard: <what she understood>`**, then runs it as if you had typed it — so
standby, the wake word, confirmations and CM routing all behave identically. The
echo is there because a voice command has a failure mode a typed one doesn't: it
can be *heard wrong*. If the transcript is wrong, correct it and send again.

Round video messages and shared audio files work the same way. Forwarded
**documents** deliberately don't — a file someone forwards you is data to look
at, not an order to run on your machine.

Two things this needs that text doesn't, both reported by `doctor`:

```bash
python era_telegram.py doctor   # "voice_ready": true means both are in place
```

| What | Why |
|---|---|
| `ffmpeg` on PATH | Telegram sends Opus-in-Ogg; it's decoded to WAV before transcription |
| A Gemini key | `gemini_transcription_api_key` in `config/api_keys.json`, falling back to the vision key, then the main one |

> Give it a **dedicated key** if you can. Quota is per-key, so a voice note
> transcribed on Era's Live-session key eats into the quota her own voice runs
> on — backwards, since a voice note is what you send when you're away from the
> machine and depending on her.

### The old Wi-Fi remote (`/remote_voice`) now needs a token

That endpoint executed any command from **any device on your Wi-Fi, with no
authentication.** It now requires a shared secret and **denies everything by default**:

- Set `ERA_REMOTE_TOKEN` to a long random string.
- Send it with each request as the `X-Era-Token` header (or a `"token"` field in the
  JSON body). Add it once to your watch/phone shortcut.
- If `ERA_REMOTE_TOKEN` is unset, the endpoint returns `403` to everyone — Era prints
  a warning at startup saying so. Telegram works from everywhere anyway, so you may
  simply not need this endpoint any more.

---

## 10. Status & housekeeping

- "**What's running right now?**" — the current job, or nothing.
- "**Is anything queued?**" — what a new rush order would wait behind.

---

## Quick-reference table

| You say / type | Era does |
|----------------|----------|
| "Plan **[brand]**'s week." | Writes the full plan — materials chosen — and stops at Gate 1 |
| "Approve the plan." | Produces each item as a draft, shows you, stops at Gate 2 |
| "Approve reels 2 and 3, change reel 1." | Produces 2 and 3; waits on your correction for 1 |
| "Approve reel 1." *(after production)* | Gate 2 — marks it `(approved)` and publishes it |
| "Change reel 1's texture." | Revises **only** reel 1 → back to Gate 2 |
| "Rewrite carousel 2's description." | Same, scoped to that caption |
| "Urgent: promo reel for **[brand]** now." | Rush lane — produces a draft immediately, doesn't post |
| "Publish **[brand]**'s approved posts now." | Force-posts `(approved)` files to IG + FB ahead of schedule |
| "The -10% is no longer valid." | Ends that promo — never used in new content again |
| "What promos are running?" | Lists each brand's active/ended offers |
| "What's scheduled?" | Lists all weekly-plan / auto-publish schedules |
| "Move **[brand]**'s **[weekly plan/auto-publish]** to **[day time]**." | Reschedules it (live, no restart) |
| "Enable/disable **[brand]** auto-publish." | Turns that schedule on/off |
| "What's running right now?" | Reports the active CM job |
| "Boost this post." / "Run a Meta ad campaign for **[brand]**." | Asks end date + budget, then creates the campaign PAUSED |
| "Approve the ad campaign." | Activates a paused campaign |

---

## Tips for clean results

- **Name the brand explicitly** in the order — it removes the "which brand?" round-trip.
- **Type** anything with exact text (a caption, a contact name, a price, a link).
- Publishing needs the file to (a) contain `(approved)` and (b) have no `.posted`
  marker. "Didn't publish?" is almost always one of those two.
- **Era decides the design.** Material, texture, hook, composition, copy — it picks
  them from the brand's swatch library and charter, and tells you what it picked.
  Steer it by correcting a named item, not by pre-specifying every detail.
- **It still won't invent a price or an offer.** A price must be in your order, and
  a discount must be a live entry in that brand's `offers:` block. Delta Cuisine's
  **-10%** is live now, so promo content uses it without asking — until you say
  *"the -10% is no longer valid"*, which Era records immediately.
- **Approving the plan is not approving the reel.** Era always shows you the finished
  asset first. Nothing reaches Instagram or Facebook without your Gate-2 approval.
- **No emoji** in any description body, for either brand. The caption footers keep
  theirs — they're the fixed brand signature, appended verbatim.
- **Delta Cuisine captions always open with `#deltacuisinegroup`** as the first hashtag.

---

## What Era remembers between runs  *(added 2026-07-20)*

Era used to start every brand job from zero: the same correction had to be given
again the following week. It no longer does. Two things now feed every run.

### 1. Your Obsidian vault

Era reads `obsidian_era\era_brain\` before every CM job and hands the relevant
notes to the run as standing context — the same way it reads the brand charter.

- **Areas/** — standing strategy (the highest-value notes; read first)
- **Resources/** — material libraries, production standards, asset conventions
- **Projects/** — active campaigns and plans
- **Archive/** — indexed but deliberately kept *out* of briefs, so a finished
  campaign can't be re-run by accident

**Write a note, and the next run obeys it.** No restart needed — the vault is
read fresh each job. A note is matched to a brand by its `brand:` frontmatter,
a brand tag, or the brand name in its filename; a note with no brand signal is
shared by every brand. **One brand's notes can never reach another's job.**

Two limits: a note can never change an account ID, handle, page or footer (the
Brand Lock always wins, and Era says so if they disagree), and a note is never
an approval — both gates still apply.

Check the connection any time: **"run doctor"**, or `python -m core.cm_vault`.

### 2. Your corrections

Every time you **correct** something ("change reel 1's texture, that laminate is
wrong") or **approve** something ("approve reel 2, that hook is perfect"), Era
records it against that brand — **in your own words, not a paraphrase** — and
replays it into every later run for that brand.

- Corrections become *"do not repeat these mistakes."*
- Approvals become *"keep doing these."*
- **Say something more than once and it is promoted to a firm rule.** Era merges
  near-identical feedback, ignoring which item you were looking at, so
  correcting the same thing on reel 1 and reel 3 counts as saying it twice.

You can read exactly what it has learned, and it is all plain text you can edit
or delete:

```
python -m core.cm_learning show delta_cuisine      # what the next run will apply
python -m core.cm_learning promote delta_cuisine   # markdown, ready to paste into a vault note
```

When a lesson proves durable, **promote it into a vault note by hand.** That
makes it deliberate and permanent, rather than something Era inferred. If Era
ever learns something wrong, delete the entry from `memory/cm_lessons.json`.

### 3. It writes back

After every job, Era appends one line to `Areas/Era_Run_Log_<brand>.md` in the
vault: when, which lane, the order, and the outcome — including an honest
*"wrote 0 files"* when a run produced nothing. Era only ever appends to its own
log notes; it never edits or deletes a note you wrote.

**Precedence when sources disagree:** Brand Lock → charter → vault notes →
learned lessons. Era flags the conflict in its summary instead of guessing.
