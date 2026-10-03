"""memory/semantic_recall.py — recall by MEANING, on top of the lexical scorer.

WHY (board item 15, acceptance point 4: "tell her Monday, ask Friday")
  Recall was token overlap between the query and the stored value/key. Measured
  against 12 realistic paraphrases of real entries, top-1 recall was 6/12:

      "what do I do for a living"   -> nothing        (the entry is `job`)
      "what should you call me"     -> nothing        (`preferred_name`)
      "how should you greet me"     -> nothing        (`startup_greeting_preference`)
      "where do I live"             -> live_ui_feed_plan, smart_glass_experiment

  That last one is the shape of the problem: a lexical scorer matched the token
  "live" in "live_ui_feed_plan". No amount of tuning fixes "living" -> "job" or
  "kid" -> "son"; those need similarity of meaning.

THE MEASUREMENT THAT DECIDED THE DESIGN
  An earlier fix in memory_manager.py stopped unanswerable queries ("showroom")
  from returning the five least-irrelevant entries scored on recency alone,
  because the model then answered confidently from unrelated notes. That fix had
  to survive this one, so the first thing measured was whether cosine similarity
  can tell "memory has an answer" from "memory does not":

      answerable   queries: cosine 0.290 .. 0.743   (min 0.290)
      unanswerable queries: cosine 0.148 .. 0.404   (max 0.404)

  They OVERLAP. Absolute cosine cannot separate them; neither can a z-score
  against the store, nor the gap to the second-best entry — all three were
  measured and all three overlap. So similarity here is trusted to ORDER
  candidates, never to decide on its own that an answer exists.

  The operating point was then swept on both things that matter at once —
  recall AND noise — rather than on similarity alone:

      cut    top-1    top-3    unanswerable queries that returned something
      0.25   10/12    11/12    3/8   <- readmits the noise the earlier fix removed
      0.30    9/12    10/12    1/8
      0.36    9/12    10/12    0/8   <- chosen: the lowest cut with ZERO noise
      0.45    8/12     9/12    0/8

  0.36 is the smallest threshold at which no unanswerable query returns
  anything. Lexical-only scores 6/12 with 0/8 noise, so the hybrid is a 50%
  recall gain for identical noise. That constraint is what picked the number;
  lowering it to buy the 10th case would undo a fix that already cost a
  debugging cycle to find.

HOW IT COMBINES: max(lexical, semantic), never a sum
  A lexical hit can therefore never be LOST by adding this — semantic can only
  rescue an entry the words missed. Degrade-never-raise, the same rule the rest
  of this repo follows.

DEGRADATION IS THE NORMAL CASE, NOT THE ERROR CASE
  Everything here returns "no semantic scores" on any failure — Ollama not
  running, the model not pulled, a timeout, a malformed reply — and recall falls
  back to exactly today's lexical behaviour. Memory must not become the thing
  that breaks when a local service is down, and the query embed runs with a
  short timeout because it sits in the middle of a spoken conversation
  (measured: ~46 ms median, with an occasional multi-second keep-alive reload,
  which is precisely what the timeout is for).

WHY LOCAL AND NOT AN API
  Recall has to work when the network is down or a Gemini quota 1011 has already
  degraded Era — losing her memory at that exact moment is the worst possible
  time. all-minilm is 46 MB and runs on the Ollama server this repo already
  talks to (core/llm/adapters/ollama_chat.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import numpy as np

from core.utils import get_memory_dir

# The model, and the two numbers the sweep above produced. COSINE_CUT is a
# noise budget, not a taste setting: see the table in the module docstring
# before changing it, and re-run tools/test_semantic_recall.py.
MODEL = os.environ.get("ERA_EMBED_MODEL", "all-minilm")
COSINE_CUT = 0.36
COSINE_SPAN = 0.34  # 0.70 - CUT: maps a cosine of 0.70+ to a full 1.0 score

# The query embed is on the conversational path; the index build is not.
_QUERY_TIMEOUT_S = 2.5
_INDEX_TIMEOUT_S = 120.0
_PROBE_TIMEOUT_S = 1.5

_lock = threading.Lock()
_probe: tuple[float, bool] | None = None  # (when, reachable) — see available()
_PROBE_TTL_S = 60.0


def _base_url() -> str:
    """The same Ollama the rest of Era uses; never a second source of truth."""
    try:
        from core.vision_config import resolve_ollama_base_url

        return resolve_ollama_base_url().rstrip("/")
    except Exception:
        return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _post(path: str, payload: dict, timeout_s: float) -> dict | None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{_base_url()}{path}", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (localhost)
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def embed(texts: list[str], *, timeout_s: float = _INDEX_TIMEOUT_S) -> np.ndarray | None:
    """L2-normalised embeddings for `texts`, or None if they cannot be had.

    Normalising here means every later comparison is a plain dot product, and no
    caller has to remember to do it.
    """
    if not texts:
        return None
    out = _post("/api/embed", {"model": MODEL, "input": texts}, timeout_s)
    vectors = (out or {}).get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        return None
    try:
        matrix = np.asarray(vectors, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if matrix.ndim != 2 or not matrix.size:
        return None
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def available() -> bool:
    """Is the embedding model actually usable right now?

    Probed, not assumed, and the answer is cached for a minute: Ollama can be
    started or stopped while Era is running, so a single startup check would be
    wrong for the rest of the session in one direction or the other.
    """
    global _probe
    if os.environ.get("ERA_SEMANTIC_RECALL", "").strip() == "0":
        # The hard off-switch, and conftest.py sets it for the whole suite.
        # Without it, every one of the thousands of tests that ranks a memory
        # would reach a real Ollama on a developer machine — embedding, writing
        # caches, and making results depend on whether a local service happens
        # to be running. The tests that mean to exercise this turn it back on.
        return False
    now = time.monotonic()
    if _probe is not None and now - _probe[0] < _PROBE_TTL_S:
        return _probe[1]
    try:
        ok = embed(["probe"], timeout_s=_PROBE_TIMEOUT_S) is not None
    except Exception as e:
        # _post already swallows the expected network errors, so reaching here
        # means something unforeseen. "Unavailable" is the only safe reading:
        # this module's contract is that it never raises into recall, and the
        # ranker's try/except must be a second line of defence, not the first.
        print(f"[Memory] [SEMANTIC] probe failed, treating as unavailable: {e}")
        ok = False
    _probe = (now, ok)
    return ok


def reset_probe() -> None:
    """Forget the cached availability answer (tests, and a manual retry)."""
    global _probe
    _probe = None


def entry_text(key: str, value: Any) -> str:
    """What gets embedded for one memory entry.

    The KEY is included, with underscores opened out, because it carries real
    meaning that the value often leaves implicit: `son_favorite_song` -> "son
    favorite song: APT" is findable by "what song does my kid like", while "APT"
    alone is findable by nothing at all.
    """
    return f"{str(key).replace('_', ' ')}: {value}"


def _cache_path():
    # Beside the store it describes, so ERA_MEMORY_DIR (which conftest.py sets)
    # isolates this cache in tests exactly as it isolates long_term.json. A
    # cache under runtime/ would be written by every test run.
    return get_memory_dir() / "embeddings_cache.json"


def _digest(text: str) -> str:
    return hashlib.sha256(f"{MODEL}\x00{text}".encode()).hexdigest()[:32]


def _load_cache() -> dict[str, list[float]]:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    vectors = raw.get("vectors") if isinstance(raw, dict) else None
    return vectors if isinstance(vectors, dict) else {}


def _save_cache(vectors: dict[str, list[float]]) -> None:
    """Atomic replace, and deliberately NOT memory.safe_store.safe_write_json:
    that keeps a versioned backup of every write, which is right for
    long_term.json and wrong for a cache — one backup per new memory entry,
    forever, of data that can always be recomputed from the model."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"model": MODEL, "vectors": vectors}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:  # a cache that cannot be written must not break recall
        print(f"[Memory] [SEMANTIC] could not persist the embedding cache: {e}")


def _vectors_for(texts: list[str]) -> dict[str, np.ndarray]:
    """Embeddings for `texts`, reusing the cache and embedding only what is new.

    Keyed by a hash of the text AND the model name, so changing `MODEL` does not
    silently mix vectors from two different spaces — which would look like a
    quality regression rather than a stale cache.
    """
    cache = _load_cache()
    wanted = {t: _digest(t) for t in texts}
    missing = sorted({t for t, d in wanted.items() if d not in cache})
    if missing:
        fresh = embed(missing)
        if fresh is None:
            # Partial is fine and better than nothing: whatever is already
            # cached still ranks, and the rest simply has no semantic score.
            if not cache:
                return {}
        else:
            for text, vector in zip(missing, fresh, strict=True):
                cache[wanted[text]] = [round(float(x), 6) for x in vector]
            _save_cache(cache)
    out: dict[str, np.ndarray] = {}
    for text, digest in wanted.items():
        vector = cache.get(digest)
        if vector:
            out[text] = np.asarray(vector, dtype=np.float32)
    return out


def score(cosine: float) -> float:
    """Cosine -> a score on the same 0..1 scale the lexical scorer uses.

    Below COSINE_CUT the answer is 0, not a small number: see the module
    docstring — a cosine of 0.404 was reached by a query with no answer in the
    store at all, so anything under the cut is noise by measurement.
    """
    if cosine <= COSINE_CUT:
        return 0.0
    return min(1.0, (cosine - COSINE_CUT) / COSINE_SPAN)


def semantic_scores(query: str, items: list[tuple[str, str, str]]) -> dict[tuple[str, str], float]:
    """{(category, key): score} for `items` = [(category, key, text)].

    Returns {} whenever semantic recall cannot contribute, which the caller must
    treat as "rank lexically, exactly as before" rather than as an error. It
    never raises: this is the optional half of recall, and the half that must
    keep working is the one that does not depend on a local service.
    """
    if not query or not items:
        return {}
    try:
        with _lock:
            return _semantic_scores(query, items)
    except Exception as e:
        print(f"[Memory] [SEMANTIC] scoring failed, ranking lexically only: {e}")
        return {}


def _semantic_scores(query: str, items: list[tuple[str, str, str]]) -> dict[tuple[str, str], float]:
    if not available():
        return {}
    query_vector = embed([query], timeout_s=_QUERY_TIMEOUT_S)
    if query_vector is None:
        reset_probe()  # it answered the probe and not the query: re-check next time
        return {}
    vectors = _vectors_for([text for _c, _k, text in items])
    if not vectors:
        return {}
    out: dict[tuple[str, str], float] = {}
    q = query_vector[0]
    for category, key, text in items:
        vector = vectors.get(text)
        if vector is None:
            continue
        value = score(float(np.dot(q, vector)))
        if value > 0.0:
            out[(category, key)] = value
    return out
