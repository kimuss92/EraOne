# Era — How to give Community-Management orders by voice

Written 2026-11-20. Every phrase in the **Say this** columns is tested: it routes to the
lane the manual says. You can also type the same words, or send them on Telegram. The full
command reference is `docs/CM_COMMANDS.md`; this manual is the short, practical version.

## 0. Quick start

1. Say the wake word (**shazam**) and give the order in one sentence. Era answers in one
   short line and works in the background: a content job can take up to 30 minutes, an
   ads draft about 6 to 10. She tells you by voice (and on Telegram) when it is done.
2. Name the **brand** (Delta Cuisine or Ceram Quartz). If you forget, she asks. She never guesses.
3. Nothing is published and no money is spent without your yes. Content has two gates:
   **Gate 1** (you approve the plan) and **Gate 2** (you approve each asset). Meta ads are
   built as **drafts**; you press **Publish** in Ads Manager yourself.
4. If you are not sure how to ask, say: *"How do I ask you to run a Meta ad?"* She answers
   from this manual.

## 1. Content orders

| Say this | What happens | Your gate |
|---|---|---|
| Plan Delta Cuisine's week | Plan for the week, nothing produced yet | Gate 1: you approve |
| Plan this week for Ceram Quartz | Same for Ceram Quartz | Gate 1 |
| Approve the plan | Produces each item as a draft, shows you, stops | Gate 2: you approve each asset |
| Approve reel 1 and 3 | Approves those assets | Gate 2 |
| Change reel 1's texture | Corrects that item and learns the preference | you re-approve |
| Change the caption of reel 2 | Rewrites that caption | you re-approve |
| Publish reel 1 now | Publishes an already approved asset right now | your yes is the order |
| Prepare the prompts for a Ceram Quartz Paletino reel | Prompts, on-screen text and caption draft for something you generate yourself; stops | Gate A: you approve |
| Urgent: promo reel for Delta Cuisine now | Rush lane: draft immediately, does not post | Gate 2 |
| Build a campaign for the new Rosieres pack | An organic content campaign (not paid) | Gate 1 |
| Make the ad creative for the Delta promo | The picture for an ad (not the paid campaign) | Gate 2 |

French works too: *"Approuve le plan"*, *"Publie le reel 1 maintenant"*, *"Modifie la texture du reel 1"*.

## 2. Meta ads (paid)

### Start a campaign (Era builds a draft)

| Say this | Notes |
|---|---|
| Run a Meta ad campaign for Delta Cuisine on the last reel, 5 euros a day for 10 days | Best form: it already has budget and duration |
| Boost the last reel for Delta Cuisine | She will ask for the end date and the budget |
| Sponsor the last reel | Same |
| Prepare a Meta ad for Delta Cuisine to get messages on WhatsApp | Messaging goal |
| Lance une campagne Meta pour Delta Cuisine | French works |

What she does, in order:
1. Asks the **end date** and the **budget** if you did not say them (every campaign, no standing budget). The account bills in **euros**: if you say dollars she tells you.
2. Gets suggestions from the crew (Tom sets the angle, Maya the copy, Nabar checks limits), then builds from your **latest published reel**.
3. For **Delta Cuisine** she builds an **Interactions** campaign (messages on WhatsApp by default) with the button **Send WhatsApp message**. A messaging ad never has "no button". Leads objective only if you ask for qualified leads through Messenger or Instagram.
4. Ends the ad set on the offer's last day if the offer ends before your duration, and says so.
5. Reads every object back from Meta and tells you the stored values.

**Your three manual steps after every Delta messaging draft** (Era's tool cannot do them):
1. Open the ad in Ads Manager, go to *Conversations > Modèles enregistrés* and select the saved **welcome template** "Démarrer des conversations 29/11/2025" (Arabic introduction, no questions and answers, so the customer still writes their own message). For Ceram Quartz and Brico Kitchen leave the template **off**.
2. Check that the Page's WhatsApp Business number is linked.
3. When you are happy, press **Publish**. Until then nothing runs and nothing is spent.

Optional: switch the ad to all three apps (Messenger, Instagram, WhatsApp) in Ads Manager. The tool takes one destination per ad set.

### Look at results

| Say this | What happens |
|---|---|
| How are my campaigns doing? | Read-only report |
| Report on my Meta ads for Delta Cuisine | Read-only report for one brand |

### Change an existing campaign

| Say this | What happens |
|---|---|
| Pause the Delta Cuisine campaign | Pauses it (confirmation first) |
| Pause the Delta Cuisine dressing reel | Finds the campaign by name and pauses it |
| Boost budget on the Taj Mahal messages campaign | Finds it by name, shows the money math |
| Rename the active Delta campaigns to the naming standard | Renames them, gives a before/after table |
| Pause ad 52596011234040 | Pauses that ad by its number (a number still works, never needed) |
| Activate ad 52596011234040 | Turns that ad on: this is spending |

Campaign names follow `[DC|CQ] - [FORMAT]_[Product] - [OBJECTIVE] - [Country][MonYY]`,
e.g. `DC - REEL_DressingAuMetre - MSG - TN_NOV26`. Say any part of it (brand, product,
objective) and the agent finds the campaign; it asks only when two match equally.
| Move the remaining budget to the Interactions campaign | Reallocates budget |
| Optimize the Interactions campaign for Delta Cuisine | Reads the data, proposes and applies safe changes |

Note: "Approve the ad campaign" is **not** an ads order (it means a content approval). To start spending, press Publish in Ads Manager or say "activate the <name> campaign".

### Calls on WhatsApp
Encourage them in the first reply you send to a customer. "Call on WhatsApp" is a button
inside the chat that only exists if WhatsApp Calling is enabled on your number and the
customer's country is eligible (not yet checked for Tunisia), so the ad never promises it.

## 3. Switch the thinking model (the "brain")

| Say this | Effect |
|---|---|
| Use Claude | Tasks use Claude (through OmniRoute) |
| Use OmniRoute | Tasks use OmniRoute's free and paid model combo |
| Use the local model | Tasks use the model on your PC (free, weakest) |
| Back to automatic routing | Era picks the best one that is up |
| Which brain are you using | She tells you what is up and what is down, and why |

If the one you asked for is down she says so and uses the strongest remaining one. Paid
models are always named before they are used. Voice conversation stays on Gemini.

## 4. Ask the crew, and make a motion reel (code-built, no video model)

Ask the crew naturally, for example *"Ask Maya for three captions for the flash sale"*.
Tom = creative direction, Maya = copy, Nabar = review, Drew = research, Laura = coordination.

**Motion reel, step by step (Option A):**
1. Say: *"Ask Tom to propose a motion design for Ceram Quartz with the Alya texture and the new textures"*, and add a reference link if you have one (Pinterest, Instagram...).
2. Tom writes a numbered storyboard. You approve it or give notes by shot number.
3. Say: *"Save it"*. Tom saves a **motion brief** in the vault Inbox. Code fills in the logos, the texture files and their real sizes, the vertical 1080x1920 format and the text rule. Era tells you the sentence to say next.
4. Double-click the **Motion Design** shortcut on your Desktop. It opens Claude Code in the brand's folder, allows the vault and the textures and logos folders, and sends the sentence for you (it picks your newest brief; to pick another: `scripts\start_motion_design.cmd ceram_quartz Alya`). By hand it is: `start motion design from this brief: "Motion brief <title>" (vault Inbox)` in Claude Code.
5. Claude asks you **first and only first**: *"Texte à l'écran : en français, ou sans texte ?"* Answer. French: it shows the French lines for your approval. No text: nothing on screen except the logo image.
6. It then builds step by step (stills, 4K render, sound) and waits for your approval at each one.

Rules: on-screen text is **French or none**, never Arabic or English. A reference link is only inspiration (structure and pacing, never another brand's logo or footage); Claude looks at it with `scripts/motion_reference.py`. The engine builds from your own logos, fonts and textures: it cannot make photoreal 3D kitchens. Textures of 410x819 px are used as hero panels only (soft in close-up). The crew only proposes: nobody says a video exists before it is rendered.

## 5. Meta changes its rules: Era keeps up

Every Monday at 11:00 (and 5 minutes after Era starts if she missed the week) a read-only
check compares Meta's help articles with the last copy. If something changed she tells you,
writes a note in the vault Inbox, and warns every ads build until the skill is updated. Each
build also re-reads Meta's live articles before creating anything. A flag means "re-read the
source", not "the rule changed".

## 6. If something goes wrong

| What you see | What to do |
|---|---|
| "I'm in standby" | Say the wake word, then repeat |
| Era says the run "asked for a login" | The job failed: say it again, or tell me to check the log |
| Era says a brain is down | Say "use OmniRoute" or "use the local model" |
| "Meta may have changed its rules" | Open the vault Inbox note, review, then ask me to update the skill |
| The draft button reads "no button" | Say "fix the button on the draft ad" with its number |
| You want to see everything | The run log is in the vault: `02 - Delta Cuisine/Era_Run_Log_delta_cuisine.md` |

## 7. What Era never does

- Publishes, activates or spends without your explicit yes.
- Uses a brand's account or Page for the other brand.
- Invents a product, price, offer or Meta rule.
- Says a draft is "running" or a video exists when it does not.
