# Era Local-Primary Model and Smart Routing Design

Date: 2026-11-18
Status: Architecture approved; awaiting written-spec review
Owner: Karim
Implementer: Codex

## 1. Outcome

Era keeps Gemini Live as its realtime voice interface while making a free local
Qwen3.5 9B model the primary engine for reasoning, coding, planning, vision,
structured output, and delegated tool work. Gemma 4 E4B remains the local fast
fallback. A working loopback OmniRoute gateway is the last-resort external
overflow route. Direct Claude profiles are not used while they are logged out.

The implementation must preserve every existing protected behavior, especially
the Gemini Live audio/session rules and the single safety/confirmation gate used
for tool execution.

## 2. Audited baseline

Hardware and runtime:

- NVIDIA RTX 3070 Laptop GPU, 8 GiB VRAM; 5.58 GiB was free during the audit.
- Intel i7-12700H and 31.7 GiB system RAM.
- Ollama 0.40.16 runs from `D:\Ollama`.
- `OLLAMA_MODELS` already resolves to `D:\ollama models` at process and user scope.
- Ollama currently has no model resident.

Installed model inventory at design time:

| Tag | Size | Architecture / quantization | Current use |
|---|---:|---|---|
| `qwen2.5vl:3b` | 3.2 GB | Qwen2-VL 3.8B, Q4_K_M | Default Ollama vision model |
| `gemma4:e4b` | 9.6 GB | Gemma 4 8B/E4B, Q4_K_M | `era.local`; retained fallback |
| `qwen25-coder-64k:latest` | 4.7 GB | Qwen2.5 Coder 7.6B, Q4_K_M | No repository reference |
| `gemma4-64k:latest` | 9.6 GB | Gemma 4 custom context tag | No repository reference |
| `all-minilm:latest` | 45 MB | embedding model | Semantic memory; retained |
| `gemma4:latest` | 9.6 GB | Gemma 4 | File processor default |
| `qwen2.5-coder:7b` | 4.7 GB | Qwen2.5 Coder 7.6B, Q4_K_M | Local classifier / lane router |
| `kimi-k2.7-code:cloud` | remote | cloud tag | No repository reference |

Provider baseline:

- Both Claude profile directories exist, but both report `loggedIn: false` after
  the loopback gateway environment is removed. They are not valid direct
  subscription fallbacks.
- OmniRoute is listening on `127.0.0.1:20128`; a minimal Messages API probe
  succeeded through `gpt-5.6-sol` in 5.287 seconds.
- Hermes Agent is listening on `127.0.0.1:8642`; the `era_one` profile has six
  registered bots, no enabled tools, and completed a read-only Laura audit turn.
- No OpenRouter or AI Gateway credential is configured. This migration must not
  invent or require one.

## 3. Requirements

### Functional

1. Pull the official Ollama `qwen3.5:9b` tag whose language model component is
   Q4_K_M and install it in the existing D: model store.
2. Keep `era.voice` on Gemini Live.
3. Add a local primary alias for Qwen and retain Gemma as a distinct fallback.
4. Route heavy text, coding, planning, vision, and delegated tool work to Qwen.
5. Route lightweight local work to Gemma when doing so does not cause needless
   model swapping.
6. Make the crew local-first with per-inference failover, preserving every bot's
   existing capability grants and confirmation boundaries.
7. Use OmniRoute only after both local models are unavailable or unusable.
8. Never route automatically to the logged-out direct Claude profiles.
9. Remove obsolete Ollama tags only after their consumers are migrated and all
   verification passes.

### Non-functional

- Only one generative model may be resident in VRAM at a time.
- All Ollama text paths use the same 12,288-token context to prevent reloads.
- A model/provider failure must be visible in logs and provenance.
- A failover must not replay a tool or side effect that may already have run.
- No secret value may be logged, committed, or printed by diagnostics.
- Startup and ordinary voice latency must not regress because of the migration.
- Deletion must use explicit `ollama rm <tag>` targets and remain recoverable by
  pulling the recorded tags again.

## 4. Architecture

```mermaid
flowchart TD
    U[Voice input] --> G[Gemini Live: realtime hearing and speech]
    G --> S{Task class / delegation policy}
    S -->|Fast voice command| GT[Gemini direct gated tools]
    S -->|Heavy reasoning, code, vision, complex sequence| Q[era.primary: Qwen3.5 9B]
    Q -->|timeout, unavailable, rate limit, auth, bad output| F[era.local: Gemma 4 E4B]
    F -->|local models unavailable| O[era.overflow: OmniRoute era-gateway]
    O -->|unavailable| E[Truthful failure; task state preserved]

    C[In-process crew] --> T{Turn tier}
    T -->|smart / critical| Q
    T -->|fast / trivial| F
```

### Alias contract

- `era.voice`: unchanged Gemini Live realtime alias.
- `era.primary`: Ollama `qwen3.5:9b`, with tools, JSON schema, streaming, vision,
  and local zero-cost reporting.
- `era.local`: Ollama `gemma4:e4b`, the fast/fallback local agent.
- `era.classify`: migrates to `qwen3.5:9b` so the removed Qwen2.5 model cannot
  force a third resident-model swap.
- `era.overflow`: direct OmniRoute adapter using the existing loopback
  `era-gateway` combo.
- `era.reason`, `era.deep`, and `era.cheap`: marked unavailable while the direct
  Claude profiles are logged out. Their historical measurements remain in the
  file for later restoration.

The registry remains the single source of truth. Call sites use aliases, never
new hard-coded provider model names.

### Voice boundary

Gemini remains the only realtime audio model. Fast spoken actions keep their
current path. Heavy spoken work is delegated through one bounded local-agent
tool. That tool runs Qwen's existing `LocalBrain` loop and returns the final
answer to Gemini for speech. The delegated brain receives the normal Era tool
surface except the delegation tool itself, preventing recursion. Every nested
tool call goes through the same dispatcher, confirmation store, grounding rules,
and completion evidence as a direct Gemini tool call.

Typed/HUD/Telegram heavy questions use the provider-neutral dispatch path.
Action-shaped requests stay on the gated tool path rather than being converted
into an ungated prose answer.

### Crew boundary

The default crew transport becomes the existing in-process transport. Its model
call becomes provider-neutral and selects a chain by turn tier:

- `smart` / `critical`: Qwen -> Gemma -> OmniRoute.
- `fast` / `trivial`: Gemma -> Qwen -> OmniRoute.

Fallback occurs around one inference call, before a returned capability call is
executed. If a capability already ran, the next inference may use a different
model with the recorded observation, but the capability is never replayed.
Hermes remains installed and manually selectable for diagnostics; it is not an
automatic replay target for a partially completed crew turn.

## 5. Ollama performance and lifecycle policy

Persistent Windows user environment:

```text
OLLAMA_MODELS=D:\ollama models
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_KEEP_ALIVE=15m
ERA_CREW_TRANSPORT=in_process
```

Rationale:

- One parallel request and one loaded model prevent Qwen and Gemma colliding in
  8 GiB VRAM. Requests queue instead of multiplying KV caches.
- Flash Attention lowers attention memory and improves throughput when supported.
- Q8 KV cache is the quality/memory compromise; Q4 is not used initially because
  tool/JSON reliability matters more than a small additional saving.
- The shared `SHARED_NUM_CTX=12288` remains unchanged. The advertised 256K native
  context is not an operational target on this GPU.
- The Ollama adapter stops hard-coding `keep_alive: 30m` and honors the user
  environment. Fifteen minutes amortizes cold starts while eventually returning
  VRAM to desktop workloads.
- GPU layer placement remains automatic. The Windows desktop already consumes a
  variable portion of VRAM, so a fixed `num_gpu` could either underuse the GPU or
  cause an out-of-memory failure. Verification records the observed CPU/GPU split.
- Thinking is off for fast/simple work and enabled only for medium/high effort
  Qwen requests. The request's effort hint, not a model-global setting, decides.

Ollama is cleanly restarted after user environment changes so its server process
inherits them. The pull is then performed with `ollama pull qwen3.5:9b` and
verified with `ollama show qwen3.5:9b` before any Era config points at it.

## 6. Failover and health rules

Advance to the next candidate on normalized model failures:

- timeout;
- transport/unavailable;
- rate limit or exhausted quota;
- authentication/billing rejection;
- model not installed / unsupported;
- empty, malformed JSON, or schema-invalid output.

Do not fail over on:

- a valid refusal or ordinary answer;
- confirmation-required, denied, cancelled, or review-paused results;
- any ambiguous condition after a side effect may already have started.

Existing Era Fabric health breakers are reused. A repeatedly failing candidate is
skipped during its breaker window, avoiding repeated timeout latency. Every result
records the chosen alias/provider/model plus `fell_back_from` and the normalized
reason. If all candidates fail, Era gives one truthful error and retains pending
task/confirmation state.

OmniRoute is represented as its own provider/alias. This removes the misleading
state where a `claude_cli` alias appears healthy because OmniRoute answered before
the logged-out CLI was tried.

## 7. Code/config surfaces

Expected implementation surfaces, subject to protected-fix checks before edits:

- `config/llm.yaml`: aliases, routing chains, provider state, and task mapping.
- `core/llm/registry.py`: provider/alias runtime metadata if required.
- `core/llm/router.py`: ordered chain policy without replacing the existing pure
  route decision contract.
- `core/llm/adapters/ollama_chat.py`: environment-aware keep-alive, effort-based
  thinking, accurate capabilities, and existing image/tool support.
- `core/llm/adapters/omniroute.py`: direct provider registration/health semantics.
- `app/era_live/dispatch.py`: provider-neutral dispatch and action-shaped guard.
- `core/local_brain.py` plus the existing tool declaration/dispatch surfaces:
  bounded local delegation with no recursive delegation tool.
- `crew/runtime.py`: tier-based local-first inference chains.
- `core/vision_config.py`, `core/empower_config.py`, `actions/file_processor.py`,
  `lane_router.py`: migrate obsolete model defaults to Qwen/registry aliases.
- focused tests and the protected-fix registry.

Unrelated PF-041/042 changes in the dirty worktree must be preserved.

## 8. Verification plan

### Model/runtime

1. `ollama show qwen3.5:9b` proves 8.95B Q4_K_M plus vision capability.
2. Run read-only probes for plain text, strict JSON, native tool selection,
   multi-step tool continuation, and one local image.
3. Record cold load, warm generation speed, peak VRAM, and CPU/GPU split.
4. `ollama ps` must never show more than one loaded generative model.
5. After the keep-alive window, verify the idle model unloads.

### Routing/failure

- Unit tests pin heavy->Qwen and fast->Gemma selection.
- Mocked timeout, rate-limit, auth, unsupported, empty, and malformed-output
  failures prove ordered fallback.
- Tests prove a valid refusal does not fall through.
- Tests prove no capability/tool is replayed across a fallback.
- Tests prove `era.voice` remains Gemini Live and retains realtime-audio capability.
- Tests prove direct Claude aliases cannot be automatically selected while logged out.

### Crew and live path

- Run registry, router, quota-failover, local-fallback, dispatch, crew registry,
  crew loop, review, and Hermes compatibility suites.
- Run one read-only request through each crew tier and inspect provenance.
- Live-check one simple spoken command remains on Gemini.
- Live-check one heavy spoken request delegates to Qwen and Gemini speaks the
  result without executing any unconfirmed action.

### Project gate

- Add PF-043 with guards that fail if the voice alias changes, Qwen ceases to be
  primary, the fallback order changes silently, or two local models may be kept
  resident by Era's configuration.
- Run `python tools/protected_fixes.py check`.
- Run `python tools/era_gate.py --changed <all changed files>` on Windows.
- Update `handoff.md` and `TODO.md` with measured proof and remaining live checks.

## 9. Cleanup and recovery

Cleanup happens only after the new primary and all guards pass. Before removal,
run a final repository reference search and `ollama list`. Keep:

- `qwen3.5:9b`;
- `gemma4:e4b`;
- `all-minilm:latest`.

Remove, when no consumer remains:

- `qwen2.5vl:3b`;
- `qwen25-coder-64k:latest`;
- `gemma4-64k:latest`;
- `gemma4:latest`;
- `qwen2.5-coder:7b`;
- `kimi-k2.7-code:cloud` (manifest cleanup; no local weight saving expected).

Use explicit `ollama rm <tag>` commands, never filesystem deletion. Ollama may
deduplicate blobs, so report actual store size before and after instead of adding
the displayed tag sizes. Recovery is `ollama pull <recorded-tag>`.

If Qwen fails acceptance, restore the previous routing configuration, keep
`gemma4:e4b` as `era.local`, restore the old referenced tags if removed, and leave
the downloaded Qwen tag installed until the failure is understood. No cleanup is
performed on a failed migration.

## 10. Architecture decisions

### ADR-001: Preserve Gemini Live as the voice transport

**Decision:** Gemini Live remains `era.voice`; local models handle delegated
intelligence rather than raw realtime audio.

**Alternatives:** Fully local Whisper/Qwen/TTS as the default; replacing the
entire voice stack.

**Consequences:** Voice stays low-latency and protected session behavior remains
stable. Era still uses a cloud voice path when it is available, but existing
local fallback preserves operation during outages.

### ADR-002: Extend the alias/provider architecture

**Decision:** Add local-primary and overflow aliases and make existing dispatch
and crew call sites provider-neutral.

**Alternatives:** Config-only alias replacement; a new standalone router.

**Consequences:** More implementation than a YAML edit, but one observable
routing contract serves Era and the crew without parallel policy engines.

### ADR-003: One loaded model and automatic GPU offload

**Decision:** Serialize local inference, load one model, share a 12,288-token
context, and let Ollama choose the safe layer split.

**Alternatives:** Keep Qwen and Gemma resident; force all Qwen layers onto GPU;
use CPU-only inference.

**Consequences:** Model switches have a cold-load cost, but the system avoids
VRAM exhaustion and remains responsive for desktop/media workloads.

### ADR-004: Local-local-external fallback order

**Decision:** Qwen/Gemma order depends on task tier; OmniRoute is last.

**Alternatives:** OmniRoute before local models; direct Claude fallback; Hermes
as an automatic replay target.

**Consequences:** Ordinary work has no API cost or external dependency. External
quality remains available during a genuine local outage without replaying a
partially completed crew turn.
