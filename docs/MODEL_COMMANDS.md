# Era — model commands

How to see which model Era is using, switch it by voice or text, and control the
autopilot that switches it for you.

Everything here works **typed or spoken** — both paths run the same grammar
(`app/text_intents.py::_model_intent`), because the live model has no routing
tool and a spoken order has to be intercepted from the transcript.

---

## What the HUD shows

Bottom-left of the window, on the same baseline as the command bar:

```
⚙ MODEL   Claude Opus 5   era.deep   PINNED
          └ the model     └ the      └ you chose it, rather
            you're on       role       than Era choosing
```

The **name** is the headline — "Claude Opus 5", not `era.deep`. The alias is
Era's word for a *role* and shows in smaller type beside it (and in the
tooltip); it answers "which lane", not "which model".

When Era could not use what was asked for, the displaced choice is struck
through in amber next to what actually ran:

```
⚙ MODEL   e̶r̶a̶.̶c̶h̶e̶a̶p̶ ↳ Gemini 3.1 Flash Live
```

A fallback never renders like a first choice. That is the whole point of the
indicator: a substitution you cannot see is one that misleads you.

---

## Switching manually

### The natural phrasing

| Say / type | Era switches to |
|---|---|
| `change your brain to opus` | Claude Opus 5 |
| `change your brain to claude` / `... to sonnet` | Claude Sonnet 5 |
| `change your brain to gemini` | Gemini 3.1 Flash Live |
| `change your brain to haiku` | Claude Haiku |
| `set your brain to fast` | Gemini 2.5 Flash Lite |
| `use your brain smart` | Gemini 2.5 Flash |
| `change the brain to local` | Qwen (local) |
| **`change your brain to auto`** | **back to Era deciding** |

`change`, `switch`, `set` and `use` all work, with `your`, `the` or neither.

### Shorter forms

| Say / type | Effect |
|---|---|
| `use claude` · `ask claude` · `hand this to claude` | Claude Sonnet 5 |
| `use opus` | Claude Opus 5 |
| `think harder` · `think this through` · `take your time` | Claude Sonnet 5 |
| `use the gemini model` · `use your fast model` | the named Gemini tier |
| `back to automatic routing` | back to Era deciding |

### One thing that is deliberately NOT a model switch

A bare **`use gemini`** or **`go local`** still means *switch the **voice***, as
it always has. Those phrases predate model routing and are intercepted first.

Say **`change your brain to gemini`** when you mean the model — the word
"brain" removes the ambiguity, which is why it is the recommended phrasing.
`tools/test_model_intent.py` asserts the two grammars never overlap.

### While pinned

A pin lasts for the **session only**. It is never written to disk: "use Claude
for this" is about the next few minutes, and a preference that outlived the
conversation would quietly send everything somewhere expensive with no obvious
cause. Relaunching clears it; so does `change your brain to auto`.

---

## Autopilot — Era choosing for herself

With no pin set, Era classifies each request and routes it. She announces the
first automatic switch of a session out loud, then stays quiet and lets the
indicator carry it.

**What moves:**

| Task class | Goes to | Why |
|---|---|---|
| `long_context` (over ~900 chars) | Claude Sonnet 5 | A pasted document is the wrong shape for a live voice session |
| `complex_planning` | Claude Sonnet 5 | Benchmarked: it got both reasoning tasks right that Gemini got wrong |
| `research_synthesis` | Claude Sonnet 5 | Same tier, same measured evidence |

**What never moves:**

Media, volume, navigation, status and short commands stay on the live Gemini
session. Measured p50: **682 ms vs 2934 ms**. "Pause" cannot afford a 2.9 s
floor, and no amount of reasoning quality changes that.

Ordinary conversation ("hello", "what do you think") is classified as *no
opinion* and stays on the voice model. The classifier is deliberately allowed to
have no opinion — one that always has an answer routes confidently and wrongly.

### Its limits

- **Allowlist**, not a filter: an unlisted class stays on the voice model.
- **Confidence gate**: thin guesses ("refactor this") are skipped.
- **25 automatic calls per day**, independent of the dollar budget — a
  misfiring classifier trips a count long before a ceiling.
- **$2.00/day budget** across everything, checked *before* each call against the
  projected total. Enforced by `core/llm/spend.py`, persisted across restarts.

---

## Turning things off

| | |
|---|---|
| Autopilot only | `ERA_LLM_AUTOPILOT=0`, or `routing.autopilot.enabled: false` |
| All non-voice dispatch | `ERA_CLAUDE_DISPATCH=0` — the master switch |
| Shadow recording | `ERA_LLM_SHADOW=0` |

`claude_dispatch` gates everything, including autopilot. One flag turns it all
off.

---

## Inspecting it

```bash
python -m core.llm.registry            # every alias, its model, cost, latency
python -m core.llm.shadow              # what routing WOULD have done
python tools/llm_bench.py --aliases era.fast,era.reason   # dry run, free
python tools/era_doctor.py             # registry + provider health
```

The shadow report records **classifications, never utterances** — task class,
word count, projected alias and cost. Enough to answer "how often, to what, at
what price"; useless for reconstructing what you said.

---

## Model costs (measured, not from a rate card)

| Alias | Model | Per call | Latency floor |
|---|---|---|---|
| `era.cheap` | Claude Haiku | $0.0163 | 2364 ms |
| `era.reason` | Claude Sonnet 5 | $0.0580 | 2756 ms |
| `era.deep` | Claude Opus 5 | $0.1840 | 2583 ms |

These are per-CALL floors, dominated by the Claude Code harness reloading on
every `claude -p` invocation — not by tokens. That is why Haiku is only ~4x
cheaper than Sonnet here despite being far cheaper per token, and why routing to
any of them is worth doing deliberately rather than by default.
