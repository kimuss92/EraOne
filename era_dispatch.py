r"""
era_dispatch.py — Era One → Claude Code dispatcher.

WHAT THIS DOES
  - Resolves which brand a spoken/typed order is about (from brands.yaml aliases).
  - Runs Claude Code in HEADLESS mode (claude -p) under that brand's own account,
    selected with CLAUDE_CONFIG_DIR. Uses your SUBSCRIPTION — no API key, no extra bill.
  - Serializes EVERY job behind one global lock, because all brands share the same
    Magnific + Adobe logins. Two jobs must never run at the same time.

REQUIREMENTS
  pip install pyyaml
  A `brands.yaml` next to this file (see the one you were given).
  Each brand's Claude account logged into its own config dir ONCE (see SETUP below).

SETUP (one time, in PowerShell)
  # Delta Cuisine account:
  $env:CLAUDE_CONFIG_DIR="C:\Users\nesri\.claude-delta"
  claude            # then /login  -> sign in with the DELTA CUISINE account
  # Freelance account (Ceram Quartz + clients):
  $env:CLAUDE_CONFIG_DIR="C:\Users\nesri\.claude-freelance"
  claude            # then /login  -> sign in with the FREELANCE account
  # Test each:  claude -p "say hi" --output-format json

USAGE FROM ERA (main.py)
  from era_dispatch import run_task
  res = run_task("prepare this week's content for Delta Cuisine", task_type="weekly")
  if res.get("need_brand"):
      speak("Which brand is this for?")
  else:
      speak(res["result"] if res["ok"] else "That failed: " + res.get("error",""))
"""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import yaml  # pip install pyyaml

from core import cm_learning, cm_vault
from core.logger import log_event, setup_logger

BRANDS_FILE = Path(__file__).with_name("brands.yaml")
# Absolute: ads runs execute in the brand's project_dir, where a relative
# `.claude/skills/...` path points at nothing.
_ADS_SKILL_PATH = Path(__file__).parent / ".claude" / "skills" / "era-ads-manager" / "SKILL.md"
# Era's in-house Meta analyst (2026-09-25) — the free replacement for HYPD/Adspirer.
_ANALYST_SKILL_PATH = Path(__file__).parent / ".claude" / "skills" / "era-meta-analyst" / "SKILL.md"
ERA_DIR = Path(__file__).parent  # exposed to each brand run via --add-dir
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")  # set full path if not on PATH

dispatch_logger = setup_logger("era_one.dispatch")
_HOSTNAME = socket.gethostname()

# ---- Status file: lets Era tell you what's running RIGHT NOW (for queue-awareness
# on rush campaigns) without guessing. Written while a job holds the lock. ----
_STATUS_PATH = Path(os.getenv("ERA_STATUS_FILE", str(Path.home() / ".era" / "era_status.json")))

# ---- The two locks that guarantee "one job at a time" -----------------------
# In-process (if Era submits jobs from multiple threads):
_GLOBAL_LOCK = threading.Lock()
# Cross-process (if Era ever spawns more than one process). Shared Magnific/Adobe.
_LOCK_PATH = Path(os.getenv("ERA_LOCK_FILE", str(Path.home() / ".era" / "era.lock")))

# ---- Staleness: a lock is a promise that a LIVE job is running. -------------
# Era is a desktop app; it gets killed, crashed and shut down mid-run routinely
# (shutdown_era, a Live-API 1008 abort, a reboot). The lockdir and status file
# outlive the process that made them, so both must be treated as CLAIMS to be
# verified, never as facts. Every reader re-checks: is the owner PID still
# alive, and is the claim younger than a job could possibly be? A lock that
# fails either test is stale and gets broken on sight — otherwise one crash
# wedges every future job behind a 30-minute doomed wait.
#
# A run can't outlive its own subprocess timeout, so anything older than
# (job timeout + slack) is dead by definition, PID liveness notwithstanding.
_DEFAULT_TIMEOUT = 1800
_STALE_GRACE = 300
_STATUS_MAX_AGE = _DEFAULT_TIMEOUT + _STALE_GRACE
# A lockdir with no owner.json is either mid-claim (a ~1ms window) or a relic
# from before owner files existed. Give the claim window a wide berth.
_ORPHAN_GRACE = 120


def _pid_alive(pid: int) -> bool:
    """Is this PID a live process on THIS machine? Windows has no os.kill(pid, 0)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # no such process (or not ours — same thing for us)
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # can't tell — assume alive, the age check still applies
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


# brands.yaml parse cache, keyed by file mtime: Hermes + dispatch call
# load_brands() several times per utterance, and re-parsing the YAML each time
# was pure waste. Editing the file still takes effect immediately (mtime moves).
_BRANDS_CACHE: tuple[float, dict] | None = None


def load_brands():
    global _BRANDS_CACHE
    try:
        mtime = os.path.getmtime(BRANDS_FILE)
    except OSError:
        mtime = -1.0
    if _BRANDS_CACHE is not None and _BRANDS_CACHE[0] == mtime:
        return _BRANDS_CACHE[1]
    with open(BRANDS_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _BRANDS_CACHE = (mtime, cfg)
    return cfg


def resolve_brand(text, cfg=None):
    """Match an order to EXACTLY one brand key. Returns None if 0 or >1 match,
    so Era can ask you instead of guessing (this is the anti-wrong-page guard)."""
    cfg = cfg or load_brands()
    t = (text or "").lower()
    hits = []
    for key, b in cfg.get("brands", {}).items():
        names = [key.replace("_", " "), (b.get("display_name") or "").lower()]
        names += [a.lower() for a in b.get("aliases", [])]
        if any(n and n in t for n in names):
            hits.append(key)
    hits = list(dict.fromkeys(hits))
    return hits[0] if len(hits) == 1 else None


def _job_is_dead(job) -> bool:
    """A status file is a claim, not a fact — the writer may have been killed."""
    started = float(job.get("started") or 0)
    if started and (time.time() - started) > _STATUS_MAX_AGE:
        return True  # outlived any possible run (subprocess timeout caps it)
    pid, host = job.get("pid"), job.get("host")
    if host and host != _HOSTNAME:
        return False  # not our machine's PID space
    if pid is None:
        # Legacy status (no PID). Trust the lock: no lockdir ⇒ nothing running.
        return not Path(str(_LOCK_PATH) + ".lockdir").exists()
    return not _pid_alive(pid)


def current_job():
    """What's running right now, if anything: {'brand','task_type','order','started'}.
    Era should check this before dispatching a rush order so it can say
    'Delta Cuisine's weekly plan is running, this will start right after'
    instead of going silent while it waits behind the lock.

    A status file left behind by a killed run is self-healed here, so Era can
    never announce a job that died hours ago as if it were still going.
    """
    try:
        job = json.loads(_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if _job_is_dead(job):
        log_event(
            dispatch_logger,
            "cm.status.stale_cleared",
            "WARNING",
            brand=job.get("brand"),
            task_type=job.get("task_type"),
            started=job.get("started"),
            pid=job.get("pid"),
        )
        _clear_status()
        return None
    return job


def is_busy():
    return current_job() is not None


def _write_status(brand_key, task_type, instruction):
    _STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATUS_PATH.write_text(
        json.dumps(
            {
                "brand": brand_key,
                "task_type": task_type,
                "order": instruction,
                "started": time.time(),
                "pid": os.getpid(),
                "host": _HOSTNAME,
            }
        ),
        encoding="utf-8",
    )


def _clear_status():
    try:
        _STATUS_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Windows refuses the unlink (WinError 32) while another process has the
        # file open — e.g. a status poll. A finished job must not turn into a
        # crash over bookkeeping; current_job() self-heals a dead job's leftover.
        pass


# ---- Post-run accountability -------------------------------------------------
# `ok=True` only means the headless subprocess exited 0 — it can still have done
# NOTHING (e.g. a produce run whose generation MCP isn't wired, or one that stopped
# at an approval gate). These two helpers give the caller something real to judge:
# a persisted transcript of exactly what the run said, and a count of how many
# files the run actually created/changed under the brand's folder.
_CM_LOG_DIR = Path(os.getenv("ERA_CM_LOG_DIR", str(Path.home() / ".era" / "cm_logs")))


def _count_recent_files(root, since_ts, cap=20000):
    """How many files under `root` were created/modified at or after `since_ts`.
    Bounded walk so a huge asset folder can never stall the dispatcher; we only
    need the 0-vs-more-than-0 signal, not an exact census."""
    n = seen = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                seen += 1
                if seen > cap:
                    return n
                try:
                    if (Path(dirpath) / fn).stat().st_mtime >= since_ts:
                        n += 1
                except OSError:
                    pass
    except OSError:
        pass
    return n


def _clear_mcp_needs_auth(config_dir, server="magnific"):
    """Headless `claude -p` skips any MCP server flagged in that account's
    `mcp-needs-auth-cache.json` — even when a VALID OAuth token is already stored
    in `.credentials.json`. Claude Code re-adds that flag whenever its own (buggy
    for Magnific's Keycloak) interactive OAuth probe fails, which would silently
    disable generation on the next scheduled run. So we strip the flag for `server`
    right before each dispatch. Never raises — a cache we can't touch must not take
    the job down. See [[era-magnific-mcp-status]]."""
    try:
        cache = Path(config_dir) / "mcp-needs-auth-cache.json"
        if not cache.exists():
            return
        data = json.loads(cache.read_text(encoding="utf-8"))
        stale = [k for k in list(data) if server.lower() in k.lower()]
        if stale:
            for k in stale:
                del data[k]
            cache.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _log_run(brand_key, task_type, instruction, returncode, stdout, stderr, wrote, started):
    """Persist a timestamped transcript of the run. Returns the path, or None if
    logging failed (logging must never take down a dispatch)."""
    try:
        _CM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(started))
        path = _CM_LOG_DIR / f"{ts}_{brand_key}_{task_type}.log"
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write("# Era CM dispatch log\n")
            f.write(
                f"brand={brand_key} task_type={task_type} returncode={returncode} "
                f"wrote_files={wrote} duration_s={time.time() - started:.0f}\n"
            )
            f.write(f"order: {instruction}\n\n")
            f.write("=== STDOUT ===\n")
            f.write((stdout or "") + "\n\n")
            f.write("=== STDERR ===\n")
            f.write((stderr or "") + "\n")
        return str(path)
    except OSError:
        return None


class FileLock:
    """Cross-process mutex via atomic directory creation (works on Windows).

    The lockdir carries an `owner.json` naming the PID that holds it. A waiter
    that finds the owner dead — or the claim older than any job could be —
    breaks the lock instead of waiting on a ghost. Without this, a single
    mid-run crash makes every later job block for the full timeout and fail.
    """

    def __init__(
        self, path, timeout=_DEFAULT_TIMEOUT, poll=1.0, stale_after=_STATUS_MAX_AGE, meta=None
    ):
        self.dir = Path(str(path) + ".lockdir")
        self.owner_file = self.dir / "owner.json"
        self.timeout, self.poll = timeout, poll
        self.stale_after = stale_after
        self.meta = meta or {}
        self.held = False

    def owner(self):
        try:
            return json.loads(self.owner_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _claim(self):
        payload = {"pid": os.getpid(), "host": _HOSTNAME, "acquired": time.time(), **self.meta}
        try:
            self.owner_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass  # lock is still ours; we just lose diagnostics

    def _is_stale(self) -> bool:
        owner = self.owner()
        if owner is None:
            # Mid-claim, or a relic from before owner.json. Judge it by age.
            try:
                age = time.time() - self.dir.stat().st_mtime
            except OSError:
                return False  # vanished under us; the mkdir retry will settle it
            return age > _ORPHAN_GRACE

        age = time.time() - float(owner.get("acquired") or 0)
        if age > self.stale_after:
            return True  # older than a job can live, whatever the PID says

        if owner.get("host") != _HOSTNAME:
            return False  # another machine's PIDs are not ours to inspect
        pid = owner.get("pid")
        if pid == os.getpid():
            return False  # our own lock — never break it from underneath us
        return not _pid_alive(pid)

    def _break_stale(self) -> bool:
        """Rename-then-delete: the rename is the atomic arbiter, so two racing
        breakers can't delete each other's freshly-taken lock."""
        victim = self.dir.with_name(f"{self.dir.name}.stale.{os.getpid()}.{time.time_ns()}")
        try:
            self.dir.rename(victim)
        except OSError:
            return False  # lost the race, or the owner released it first
        shutil.rmtree(victim, ignore_errors=True)
        return True

    def __enter__(self):
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        while True:
            try:
                self.dir.mkdir()  # atomic; raises if already held
            except FileExistsError:
                if self._is_stale():
                    stale_owner = self.owner()
                    if self._break_stale():
                        log_event(
                            dispatch_logger,
                            "cm.lock.stale_broken",
                            "WARNING",
                            lock=str(self.dir),
                            stale_owner=stale_owner or "no owner.json (pre-upgrade relic)",
                        )
                    continue  # retry immediately — no reason to sleep
                if time.time() - start > self.timeout:
                    raise TimeoutError(
                        "Another Era job is still running (shared Magnific/Adobe)."
                    ) from None
                time.sleep(self.poll)
            else:
                self._claim()
                self.held = True
                return self

    def __exit__(self, *exc):
        if not self.held:
            return
        self.held = False
        try:
            self.owner_file.unlink()
        except OSError:
            pass
        try:
            self.dir.rmdir()
        except OSError:
            pass


def unlock(force=False):
    """Break the global lock and clear the status file. `force` skips the
    staleness check — only for an operator who knows nothing is running."""
    lock = FileLock(_LOCK_PATH)
    if not lock.dir.exists():
        _clear_status()
        return {"broken": False, "reason": "no lock held"}
    owner = lock.owner()
    if not force and not lock._is_stale():
        return {"broken": False, "reason": "lock is held by a live job", "owner": owner}
    broken = lock._break_stale()
    _clear_status()
    return {"broken": broken, "owner": owner}


def _routing_summary(brand, defaults=None):
    """Render this brand's exact destinations for the Brand Lock (from the registry).
    Each platform's TOOL resolves in order: this brand's own <platform>_tool
    override -> the global defaults block -> "zernio" as a last resort. Never
    assume Zernio outright — a brand may route any platform through Composio
    instead (e.g. Ceram Quartz: IG+FB via Composio; DC: FB via Composio through
    the global default, with no brand-level override needed)."""
    defaults = defaults or {}
    lines = []
    for name, p in (brand.get("platforms") or {}).items():
        who = p.get("handle") or p.get("page_name") or ""
        tool = brand.get(f"{name}_tool") or defaults.get(f"{name}_tool") or "zernio"
        if tool == "composio":
            ident = (
                p.get("composio_page_id")
                or p.get("composio_connected_account_id")
                or p.get("zernio_account_id")
                or ""
            )
        else:
            ident = p.get("zernio_account_id") or ""
        # Instagram's post API needs the numeric IG Business user id in ADDITION to
        # the connected-account id (which is only the auth). Surface both so the
        # publisher never has to guess or re-look-it-up (and never falls back to
        # Composio's default IG account, which belongs to a different brand).
        extra = ""
        if name == "instagram" and p.get("composio_ig_user_id"):
            extra = f", ig_user_id={p['composio_ig_user_id']}"
        lines.append(f"    - {name}: {who}  (via {tool}, id={ident}{extra})")
    return "\n".join(lines), (brand.get("routing") or {})


def _has_arabic(text):
    """True if the text contains any Arabic-script character (U+0600–U+06FF main
    block, plus the Supplement/Extended-A/Presentation ranges). Used to pick the
    caption footer that matches the description language."""
    if not text:
        return False
    for ch in text:
        o = ord(ch)
        if (
            0x0600 <= o <= 0x06FF
            or 0x0750 <= o <= 0x077F
            or 0x08A0 <= o <= 0x08FF
            or 0xFB50 <= o <= 0xFDFF
            or 0xFE70 <= o <= 0xFEFF
        ):
            return True
    return False


def select_caption_footer(brand, description):
    """Return the caption footer whose language MATCHES the description body.

    Arabic description -> caption_footer_ar (if the brand defines one);
    French/Latin description -> caption_footer. Falls back to the French footer
    when no Arabic footer is configured, or to the Arabic one if that's all the
    brand has. Returns "" when the brand has no footer at all."""
    fr = (brand.get("caption_footer") or "").strip()
    ar = (brand.get("caption_footer_ar") or "").strip()
    if _has_arabic(description):
        return ar or fr
    return fr or ar


def _render_design_rules(rules, _indent=0):
    """Flatten a brand's design_rules mapping into readable, indented bullet lines
    so the headless job sees the exact material/harmony laws inline in its brief
    (not only via the interior-design skill, whose auto-discovery isn't guaranteed
    across --add-dir). Purely structural — works for any nesting the yaml uses."""
    pad = "  " * _indent
    lines = []
    if isinstance(rules, dict):
        for k, v in rules.items():
            label = str(k).replace("_", " ")
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}- {label}:")
                lines.append(_render_design_rules(v, _indent + 1))
            else:
                lines.append(f"{pad}- {label}: {v}")
    elif isinstance(rules, list):
        for item in rules:
            if isinstance(item, (dict, list)):
                lines.append(_render_design_rules(item, _indent))
            else:
                lines.append(f"{pad}- {item}")
    else:
        lines.append(f"{pad}- {rules}")
    return "\n".join(x for x in lines if x)


def _offers_note(brand):
    """Standing promotions the CM may use without asking. An offer stays usable
    until the user says it stopped (brands.yaml -> offers[].active: false)."""
    live = [o for o in (brand.get("offers") or []) if o.get("active")]
    if not live:
        return ""
    lines = []
    for o in live:
        where = ", ".join(o.get("usable_in") or []) or "promo content"
        lines.append(f"  • {o.get('label', '')} — {o.get('description', '')} (use in: {where})")
    return (
        "=== LIVE OFFERS — active right now, use them, never ask ===\n"
        + "\n".join(lines)
        + "\nThese are CURRENT. Do not ask me whether an offer is still running, and "
        "do not omit it from promo content out of caution — it is live until I tell "
        "you it stopped.\n\n"
    )


def _caption_rules_note(brand):
    """Hard caption-body constraints (emoji policy, mandatory first hashtag)."""
    rules = brand.get("caption_rules") or {}
    if not rules:
        return ""
    lines = []
    if rules.get("no_emoji"):
        lines.append(
            "  • NO EMOJI anywhere in the description/caption body you write. Zero. "
            "The caption FOOTER above is the brand signature and is appended VERBATIM "
            "— if it contains emoji, leave them exactly as they are."
        )
    first = (rules.get("first_hashtag") or "").strip()
    if first:
        lines.append(
            f"  • The FIRST hashtag of every caption must be {first} — always, no "
            "exceptions, before any other hashtag."
        )
    if not lines:
        return ""
    return "=== CAPTION RULES — HARD ===\n" + "\n".join(lines) + "\n\n"


def _magnific_note(brand):
    """The Magnific generation contract: which flows this brand may run, what a run
    is allowed to cost, and the two traps that silently produce wrong output.

    Rendered only for lanes that generate. The content is NOT decorative — each
    block exists because of something verified against the live MCP server on
    2026-07-29:

    * The account is Premium+ "unlimited", but `unlimitedAppliesHere` is FALSE in
      MCP sessions, so every generation really bills against a finite balance. The
      headless run cannot see a bill, so the ceiling has to be stated to it.
    * Magnific's Brand Kit — the feature whose entire job is on-brand generation —
      is unreachable: `brand_kit_list`/`brand_kit_get` are not exposed, so there is
      no way to obtain the `brandKitId` that `images_generate` advertises. A guessed
      id doesn't error, it changes the output. Hence the explicit ban.
    * Flows are single-shot input->output and cannot hold one product's identity
      across shots, which is exactly the drift the master-anchored wardrobe pipeline
      exists to prevent — so the per-brand `never` clause is carried through verbatim.
    """
    mag = brand.get("magnific") or {}
    if not mag:
        return ""

    approved = mag.get("approved_flows") or []
    banned = mag.get("banned_flows") or []
    ratios = mag.get("ad_ratios") or []
    ceiling = mag.get("credit_ceiling_per_run")

    lines = []
    if ceiling:
        lines.append(
            f"  • COST CEILING: {ceiling} credits per run. The account shows "
            '"unlimited", but unlimited does NOT apply to this session — every '
            "generation really bills a finite balance."
        )
    if mag.get("require_simulate"):
        lines.append(
            "  • ALWAYS preflight. `simulate_flows` (and `simulate_cost`) are FREE and "
            "read-only. Call one BEFORE any `flows_run`, `images_generate` or "
            "`video_generate`. If the estimate exceeds the ceiling, DO NOT run it — "
            "say what it would have cost and what you would do instead, then stop. "
            "Never spend past the ceiling to 'finish the job'."
        )
    if approved:
        lines.append("  • APPROVED FLOWS — the only ones you may run for this brand:")
        for f in approved:
            use = f.get("use", "")
            lines.append(
                f"      - {f.get('name', '')} (id {f.get('id', '')}, "
                f"{f.get('credits', '?')} credits){' — ' + use if use else ''}"
            )
    if banned:
        lines.append("  • BANNED FLOWS — never run these, whatever the order says:")
        for f in banned:
            lines.append(f"      - id {f.get('id', '')} — {f.get('reason', '')}")
    if ratios:
        lines.append(
            f"  • AD FORMAT FAN-OUT: once ONE visual is approved-looking, use "
            f"`design_auto_resize(creationIdentifier=..., aspectRatios={list(ratios)})` "
            "to produce every placement from that single image rather than generating "
            "each size separately — it is one cheap call instead of N generations, and "
            "it keeps the framing consistent across placements. Use "
            "`design_auto_layers` only when the copy genuinely has to sit in editable "
            "layers."
        )
    never = (mag.get("never") or "").strip()
    if never:
        lines.append(f"  • NEVER: {never}")

    return (
        "=== MAGNIFIC — GENERATION ENGINE, COST-GATED ===\n"
        "Magnific is a generation ENGINE, not a brand authority. It cannot check your "
        "brand for you — the DESIGN RULES above are the only thing that does.\n"
        + "\n".join(lines)
        + "\n  • BRAND KIT IS UNAVAILABLE. Never pass `brandKitId`, `templateSlug`, "
        "`validationRunId` or `validationScenarioKey` to `images_generate`. Those "
        "parameters exist in the schema, but the tools that issue their ids "
        "(`brand_kit_list` / `brand_kit_get`) are NOT exposed here, so any value you "
        "use is invented — and an invented id does not error, it silently changes the "
        "output. On-brand comes from the DESIGN RULES block, nowhere else.\n"
        "  • MAGNIFIC WRITES NO TEXT. Every caption, hook, headline and on-image line "
        "is YOURS to write, from the caption rules, the brand charter, the vault notes "
        "and the lessons above. Never let a generated image carry invented brand copy.\n"
        "  • Normal order of work: `flows_get` (read the input spec) -> `simulate_flows` "
        "(free) -> `flows_run` -> `flows_wait` -> `creations_show`. Chain by creation "
        "`identifier`, never by webUrl.\n\n"
    )


def _render_ads_audiences(audiences):
    """Render brands.yaml's ads.audiences entries into brief prose.

    Kept separate from the organic content brief's material/texture rendering
    because ad targeting has its own vocabulary (geo/age/interests/retargeting)
    that would be noise in a kitchen render's design_rules and vice versa.
    """
    if not audiences:
        return ""
    blocks = []
    for aud in audiences:
        lines = [f'  "{aud.get("name", "unnamed audience")}"']
        if aud.get("insight"):
            lines.append(f"    Report insight: {aud['insight']}")
        if aud.get("geo"):
            lines.append(f"    Geo: {aud['geo']}")
        if aud.get("age_range"):
            lines.append(f"    Age: {aud['age_range']}")
        if aud.get("languages"):
            lines.append(f"    Languages: {', '.join(aud['languages'])}")
        if aud.get("interests"):
            lines.append(f"    Interests: {', '.join(aud['interests'])}")
        if aud.get("secondary_interests"):
            lines.append(f"    Secondary layer: {aud['secondary_interests']}")
        if aud.get("objective_note"):
            lines.append(f"    Objective: {aud['objective_note']}")
        if aud.get("retargeting"):
            lines.append(f"    Retargeting: {aud['retargeting']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _shared_account_page_lock(ads, brand, cfg=None):
    """PAGE LOCK for an ad account that more than one brand spends from.

    Delta Cuisine and Brico Kitchen (same group) share act_33183820 and one card
    (Karim, 2026-09-25). The account lock alone cannot tell their campaigns
    apart — the Facebook Page each campaign promotes can. Without this, a
    Delta order read and changed Brico campaigns as if they were Delta's.
    Returns "" when the account is not shared. Never raises.
    """
    try:
        cfg = cfg or load_brands()
        account_id = ads.get("meta_ad_account_id")
        own_page = str(ads.get("page_id") or "")
        others = []
        for b in (cfg.get("brands") or {}).values():
            o = b.get("ads") or {}
            if (
                account_id
                and o.get("meta_ad_account_id") == account_id
                and str(o.get("page_id") or "") != own_page
            ):
                others.append(f"{b.get('display_name')} (page {o.get('page_id')})")
        if not others:
            return ""
        return (
            "=== SHARED AD ACCOUNT — PAGE LOCK ===\n"
            f"{account_id} is shared with: {', '.join(others)} — same group, one "
            "payment card. For THIS order you may only create, change or judge "
            f"campaigns / ad sets / ads that promote page {own_page} "
            f'("{brand.get("display_name")}"). Before touching any object, read the '
            "page it promotes; if it is another brand's page, leave it untouched. "
            "In reports, list only this brand's campaigns and give the other brand's "
            "spend as ONE line (shared wallet), never mixed into this brand's numbers. "
            "Budget moves between brands happen only when my order names both.\n\n"
        )
    except Exception:  # noqa: BLE001 - a lock-text failure must not block the run
        return ""


def _ads_scope_rule(ads):
    """Brands Karim only BOOSTS (Brico Kitchen): someone else makes their content;
    his whole job is paid campaigns on posts already published (2026-09-25)."""
    if (ads or {}).get("scope") != "boost_only":
        return ""
    return (
        "=== BOOST-ONLY BRAND ===\n"
        "Karim does NOT create content for this brand — another team does. Your only "
        "job is paid campaigns on posts ALREADY PUBLISHED on its Page. Never write, "
        "generate, edit or publish posts, captions or visuals, and never plan organic "
        "content. Use each post exactly as published (object_story_id). The post's own "
        "price and offer ARE the brand's published offer, so they may run (this "
        "overrides the price line in the messaging rule) — but read its validity date "
        "('valable jusqu'au 30/09', 'صالح إلى 30/09') and never let an ad run past it: "
        "end date <= offer expiry. If the offer has expired, say so and stop.\n\n"
    )


def _ads_account_lock(ads, brand, restate=True):
    """The Ad Account Lock text, shared by every ads mode.

    One copy on purpose: build, report and manage all touch real money, and a
    lock that reads differently in each brief is a lock that drifts. Wrong-account
    is the one mistake here that no later step can undo.

    `restate` asks the model to state the lock back before creating anything —
    right for build, wrong for report/manage: their final message is SPOKEN, and a
    restated lock (ids, config paths) buried the actual answer under a wall of
    text. There the lock still binds every tool call; it just isn't recited.
    """
    account_id = ads.get("meta_ad_account_id")
    excluded = ads.get("meta_ad_account_excluded") or []
    excluded_lines = "\n".join(
        f'  - {e.get("id")} ("{e.get("name", "")}") — {e.get("note", "excluded")}' for e in excluded
    )
    return (
        _shared_account_page_lock(ads, brand)
        + _ads_scope_rule(ads)
        + (
            f"=== AD ACCOUNT LOCK — the ONLY account you may touch ===\n"
            f"    meta_ad_account_id: {account_id}\n"
            f"    page_id: {ads.get('page_id', '')}\n"
            f"    notify: {brand.get('notify_email', '')}\n"
            + (
                f"NEVER use any of these EXCLUDED accounts, even if a lookup surfaces "
                f"one as a plausible match:\n{excluded_lines}\n\n"
                if excluded_lines
                else "\n"
            )
            + "NEVER let a tool default to whatever ad account is 'current' — always pass "
            f"{account_id} explicitly. "
            + (
                "State this Ad Account Lock back to me before creating anything.\n\n"
                if restate
                else "Do NOT recite this lock in your final message.\n\n"
            )
        )
    )


def _render_ads_goals(ads):
    """What the brand is optimising for + the account's own baselines, so a
    report or a scale/stop call is judged against Karim's goals and history
    rather than generic vanity metrics. Empty when brands.yaml has neither."""
    goals = ads.get("goals") or {}
    base = ads.get("baselines") or {}
    if not goals and not base:
        return ""
    lines = ["=== WHAT THIS BRAND IS OPTIMISING FOR (brands.yaml → ads.goals / baselines) ==="]
    if goals.get("primary"):
        lines.append(f"PRIMARY GOAL: {goals['primary']}")
    if goals.get("secondary"):
        lines.append(f"SECONDARY GOAL: {goals['secondary']}")
    if goals.get("note"):
        lines.append(f"NOTE: {goals['note']}")
    if base:
        cur = ads.get("currency", "")
        lines.append(f"ACCOUNT BASELINES (this account's own history, {cur}):")
        for key, val in base.items():
            lines.append(f"  - {key.replace('_', ' ')}: {val}")
    return "\n".join(lines) + "\n\n"


# Messaging ads: Karim's standing rule (2026-09-19, memory era-ads-no-auto-greeting).
# Shared by build and manage because both can create or edit an ad that opens a chat.
_ADS_MESSAGING_RULE = (
    "=== MESSAGING ADS — NO AUTOMATED MESSAGE (Karim's standing rule) ===\n"
    "On any ad that opens Messenger / WhatsApp / Instagram DM, the customer types "
    "their OWN request. Do not accept Meta's default 'Conversations' template "
    "(the 'Bonjour <name> !' greeting, the AI-suggested questions, a pre-filled "
    "customer message). Leave 'Sélectionner un modèle' UNSELECTED. If the editor "
    "refuses to save without a greeting, use one short neutral line and NO "
    "questions and NO pre-filled message. NEVER attach a saved template that "
    "contains prices or auto-answers — old saved templates quote obsolete prices.\n"
    "Before using an existing post as the creative, read its text: if it names a "
    "price or an offer that is not in this brief, do not use it.\n\n"
)


def _ads_creative_source_rule(ads):
    """Karim's standing rule (2026-09-25): an ad's creative is ALWAYS content the
    brand already published — never a file from the vault or a project folder.

    Why: asked for "the last story" (a Tej Mahal reel on Instagram + the Page),
    Era searched local folders for "Taj Mahal", found nothing, and built the ad on
    an unrelated vault reel (Perla). The published post was one API call away.
    Shared by build and manage because both can create a new ad."""
    page_id = (ads or {}).get("page_id", "")
    return (
        "=== CREATIVE SOURCE — PUBLISHED CONTENT ONLY (Karim's standing rule) ===\n"
        "The ad's creative MUST be a post this brand has ALREADY PUBLISHED on its "
        "Facebook Page or Instagram. NEVER upload, generate, or pick a file from the "
        "vault, the project folder, or the texture library — those are NOT ad "
        "creatives, whatever their filename says.\n"
        "Find the post (Composio tools, read-only):\n"
        f"  - FACEBOOK_GET_PAGE_POSTS page_id={page_id} (newest first; `id` is "
        "already the <page_id>_<post_id> the ad needs)\n"
        "  - INSTAGRAM_GET_IG_USER_MEDIA ig_user_id='me' and "
        "INSTAGRAM_GET_IG_USER_STORIES (active stories)\n"
        "Match on the product name in the caption — spellings vary (e.g. 'Taj "
        "Mahal' is captioned 'Tej Mahal'), so match loosely. 'The last story / "
        "post / reel' means the NEWEST one. A story is usually a reshare of a "
        "feed post or reel: use the Page post of that same content.\n"
        "Build the creative with facebook-ads ads_create_creative using "
        "object_story_id = that Page post id (its video and caption are reused "
        "as-is). If no published post matches, say so in one sentence and STOP — "
        "never substitute a different product or an unpublished file.\n\n"
    )


def _build_ads_ops_brief(mode, brand_key, brand, instruction, vault_note, lessons_note):
    """Brief for reading or changing campaigns that ALREADY EXIST (added 2026-09-19).

    Separate from the create brief for the same reason that one is separate from
    the organic brief: its instructions contradict. The create brief says
    "everything is created PAUSED, stop and ask for budget"; a report must not
    change anything at all, and a manage order must be able to act on a running
    campaign. Sharing one text would leave a rule in force that the mode has to
    ignore — and a rule the model is told to ignore is one it half-obeys.

    `mode` is "report" (read-only) or "manage" (confirmation-gated changes).
    """
    ads = brand.get("ads") or {}
    cur = ads.get("currency", "")
    head = (
        f'You are the senior paid-media manager for "{brand.get("display_name")}" '
        f"(brand key: {brand_key}), running the Meta Ads (Facebook/Instagram) account. "
        f"Quote amounts in the currency Meta returns for this account (brands.yaml says "
        f"{cur}; if Meta says otherwise, use Meta's and say so). Read {_ADS_SKILL_PATH} "
        "(sections 'Reporting and management — the senior CM playbook' and "
        f"'Performance playbook') first, then analyse with {_ANALYST_SKILL_PATH} "
        "(Era's own Meta analyst — never call paid analysis plugins).\n\n"
    )
    lock = _ads_account_lock(ads, brand, restate=False)
    goals = _render_ads_goals(ads)

    if mode == "report":
        body = (
            "=== MODE: REPORT — READ-ONLY ===\n"
            "Do NOT create, edit, pause, activate, rename or delete anything. If a "
            "change is warranted, RECOMMEND it; Karim gives the command.\n\n"
            "=== KEEP IT LEAN — the gateway does NOT cache, so every tool result is "
            "re-read on every step and a long sweep gets killed before it finishes ===\n"
            "Make the FEWEST, BROADEST calls that answer the question. Do NOT walk every "
            "campaign, ad set and ad. The default report is CAMPAIGN-LEVEL only:\n"
            "  1. ONE insights call at level=campaign for the whole account, last 7 days, "
            "asking only for: campaign_name, objective, spend, impressions, reach, results/"
            "actions, and cost per result. That one response covers every campaign at once.\n"
            "  2. From THAT, keep only campaigns that delivered in the last 7 days (spend or "
            "impressions > 0) plus any active one starting within 3 days. Ignore the long "
            "tail of old paused/zero-spend campaigns, and never mistake an unpublished draft "
            "for a running one.\n"
            "  3. Drill into ad-set or ad level, or pull age/gender/region/placement "
            "breakdowns, for AT MOST the one or two campaigns you are about to call NEEDS "
            "CHANGES or FAILING — never for the whole account, and never 'to be thorough'.\n"
            "Stop pulling as soon as you can give one verdict per live campaign. If a tool "
            "returns an error, retry it ONCE; if it still fails, say so plainly and stop — "
            "do not loop. Metrics arrive as strings; cast before comparing. State the one "
            "attribution window you used.\n\n"
            "JUDGE each campaign against ITS OWN objective and the baselines above — an "
            "awareness or follower campaign is never judged on conversations. Give EXACTLY "
            "ONE verdict per campaign:\n"
            "  SUCCESS       beating baseline on its goal with enough volume to trust it\n"
            "  ON TRACK      inside baseline; not enough volume yet to say more\n"
            "  TOO EARLY     under ~50 results a week, under 3 full days, or still learning — "
            "say exactly what to wait for and until when. Never call an early campaign a failure.\n"
            "  NEEDS CHANGES delivering but off baseline — name the ONE main cause "
            "(audience, creative, copy, offer, budget too thin, frequency/fatigue, placement, "
            "schedule/reply time, tracking) WITH the number that proves it\n"
            "  FAILING       not delivering, or far off baseline after enough data\n"
            "For messaging campaigns, cheap conversations are only a proxy for sales: ask "
            "Karim, in ONE line, how many became quotes or showroom visits. Do not interrogate.\n\n"
            "RECOMMENDATIONS: each is (what, why with a number, expected effect, cost) AND is "
            "written as a ONE-LINE COMMAND Karim can say back verbatim and that is complete on "
            "its own — campaign name plus the exact change (e.g. 'extend the SAVANA campaign to "
            "30 Sep') — because the next order reaches you with no memory of this report. Rank "
            "them. Even a SUCCESS gets at least one improvement idea. Also look ACROSS "
            "campaigns: audience overlap between them, budget worth moving, what to stop.\n\n"
            "SAVE the full report as a dated markdown file in "
            f"{brand.get('project_dir', '')}\\ads_reports\\ (create the folder if missing). "
            "Do not email it unless the order says so. END with a spoken-length summary "
            "(at most 6 sentences): each campaign's verdict in a few words, the single best "
            "next action, and the exact command to say for it.\n\n"
        )
    else:  # manage
        body = (
            (
                "=== MODE: MANAGE — CHANGE EXISTING CAMPAIGNS (confirmation-gated) ===\n"
                "You may: extend or shorten the end date; raise or lower a budget (daily or "
                "lifetime); pause or resume; duplicate (the copy is created PAUSED); rename; "
                "change locations / age / audience; add a NEW ad or ad set (PAUSED). Never edit "
                "a running ad's creative in place — add a new ad instead.\n\n"
                "TARGET: act ONLY on the campaign(s) the order names. If it names none, or the "
                "name matches more than one, list the candidates (name, status, budget) and STOP.\n\n"
                "CONFIRMATION — one gate, and it is stateless because you cannot remember an "
                "earlier message:\n"
                "  - A COMPLETE COMMAND (names the campaign AND the exact change, e.g. 'raise the "
                "CQ Ventes budget to $6/day', 'pause the followers campaign') IS the confirmation: "
                "do it.\n"
                "  - Anything vaguer ('scale the winner', 'give it more budget', 'extend it') is NOT "
                "confirmed. Work out the exact change, show the money math, and STOP with a one-line "
                "command Karim can say back verbatim. Do not change anything.\n"
                "  - Reductions and pauses (less spend, less exposure) never need confirmation once "
                "the target is unambiguous.\n\n"
                f"SPEND GUARD — before any increase (budget up, end date later, resume) state: current "
                f"→ new {cur} per day, added spend per day, TOTAL added through the end date, and "
                "the new total commitment.\n\n"
                "SCALING DISCIPLINE: a rise above +25% at once (or above +35% within a week) can "
                "reset Meta's learning. If the command asks for more, do it but warn in one sentence; "
                "if YOU are choosing the number, cap it at +25%. Do not make significant edits "
                "(audience, goal, big budget cuts) to an ad set still in its learning phase unless "
                "the order is explicit or the ad is not delivering.\n\n"
                "NEVER: touch another account or a campaign the order did not name; activate "
                "anything created in this same run; use 'review and publish' / publish-all (the "
                "account holds many unrelated unpublished drafts — edit only the named object).\n\n"
                "VERIFY: after every change read it back from Meta and confirm the value. Report "
                "before → after and the resulting total commitment. If the read-back disagrees or "
                "the call errors, say so plainly — do not retry blindly.\n\n"
                "THEN act like a senior CM: add 1–2 sentences on what you would do next, with a "
                "ready-to-say command. If the order also asked for results, give the short verdict "
                "(SUCCESS / ON TRACK / TOO EARLY / NEEDS CHANGES / FAILING) first.\n\n"
            )
            + _ADS_MESSAGING_RULE
            + _ads_creative_source_rule(ads)
        )
    return (
        head
        + lock
        + goals
        + body
        + f"{vault_note}"
        + f"{lessons_note}"
        + "=== YOUR FINAL MESSAGE IS SPOKEN ALOUD ===\n"
        "A voice assistant reads your final message out verbatim, so write at most "
        "six plain sentences: no markdown, bullets, tables, file paths, account or "
        "campaign ids, or code. Detail belongs in the saved file, not here. If you "
        "could not pull REAL data from Meta (a connection or sign-in error, empty "
        "results), say that in your FIRST sentence — never present an empty pull as "
        "a report, and never assign a verdict without numbers.\n\n"
        + f"TASK TYPE: ads ({mode})\n"
        + f"ORDER FROM USER: {instruction}\n"
    )


def _build_ads_brief(brand_key, brand, instruction, vault_note, lessons_note):
    """The Meta Ads brief — deliberately a SEPARATE brief, not a section bolted
    onto build_brief's organic-content one. Two reasons:

    1. The organic brief's closing instruction is "DECIDE, DO NOT INTERROGATE
       ... NEVER stop mid-run to ask me which texture, which material" — that
       is the exact OPPOSITE of what an ads run must do: it MUST stop and ask
       for the end date and the budget on every single campaign, no exceptions
       (Karim's explicit correction, 2026-07-20 — see the ads.budget_policy
       comment in brands.yaml). Appending an ads section to the same brief
       would leave that contradictory "never ask" instruction in place.
    2. Everything else in the organic brief — caption footer, credit
       discipline, PLAN-FIRST/PUBLISH MODE, design_rules — is either
       irrelevant to ad-building or actively confusing there.

    vault_note/lessons_note are still passed through: past performance
    findings and brand-voice preferences are genuinely useful for ad copy,
    unlike the organic-specific sections above.
    """
    ads = brand.get("ads") or {}

    # Reading or changing campaigns that already exist is a different job from
    # building one, with instructions that contradict this brief's (see
    # _build_ads_ops_brief). Anything that names neither is a build, so the
    # original behaviour is the default and a detector failure can only fall
    # back to it — never into a mode that is allowed to change spend.
    try:
        from app.cm_intents import ads_mode

        mode = ads_mode(instruction)
    except Exception:  # noqa: BLE001 - degrade to the original build behaviour
        mode = "build"
    if mode in ("report", "manage"):
        return _build_ads_ops_brief(mode, brand_key, brand, instruction, vault_note, lessons_note)

    cur = ads.get("currency") or "TND"
    audiences_block = _render_ads_audiences(ads.get("audiences"))

    return (
        f'You are the senior paid-media manager for "{brand.get("display_name")}" '
        f"(brand key: {brand_key}), building a Meta Ads (Facebook/Instagram) campaign.\n\n"
        "Full campaign-construction rules (structure, audience, placements, "
        "bidding, creative, field limits, the Performance playbook) are in "
        f"{_ADS_SKILL_PATH} — read it before building anything.\n\n"
        + _ads_account_lock(ads, brand)
        + "=== BUDGET — YOU MUST ASK, EVERY SINGLE CAMPAIGN, NO EXCEPTIONS ===\n"
        "There is NO standing budget cap for this brand. Before creating anything, "
        "even paused, ask me TWO questions for THIS campaign specifically:\n"
        '  1. "When should this campaign end?" (a date, or a duration)\n'
        '  2. "What\'s the budget?" (daily or total/lifetime, my choice)\n'
        "Given either form, compute the other side and STATE the total spend implied "
        f"before creating anything (e.g. '14 days at 20 {cur}/day = 280 {cur} total.' — "
        "a statement, not a question: my answer IS the confirmation, never ask me to "
        "confirm it again). If I have not answered both questions for this campaign, do NOT "
        "create it yet. If my ORDER already gives both (e.g. '14 days, 3 dollars a "
        "day'), that IS my answer: state the implied total and build it PAUSED in "
        "this same run — do not ask me to repeat it; if I gave a range, take the "
        "lower figure and say so. This is the one hard stop-and-ask in this entire "
        "brief — everything else about the campaign (objective, copy variations, "
        "targeting, creative selection) is your professional call to make and state, "
        "never a question: it is created PAUSED, so I review it before anything "
        "spends.\n\n"
        "=== ALWAYS PAUSED ===\n"
        "Campaign, ad set, ad — created PAUSED, no exceptions. Present a review "
        "summary and STOP. Activate only after my explicit approval, and never in "
        "the same run that created it.\n\n"
        + _ADS_MESSAGING_RULE
        + _ads_creative_source_rule(ads)
        + _render_ads_goals(ads)
        + (
            f"=== NAMED AUDIENCES (from real report data) ===\n{audiences_block}\n\n"
            if audiences_block
            else ""
        )
        + f"{vault_note}"
        + f"{lessons_note}"
        + f"TASK TYPE: ads\n"
        f"ORDER FROM USER: {instruction}\n\n"
        "Build the campaign per the rules above — ask for end date + budget only if "
        "my order does not already give them — create it PAUSED from an already-"
        "published post, then end with a short status summary "
        "I can read aloud (what was built, the ad account used, and that it's paused "
        "awaiting my approval)."
    )


def build_brief(brand_key, brand, task_type, instruction, defaults=None, all_brands=None):
    """The structured brief Era hands to Claude. On-brand, gated, with the exact
    destinations embedded so it can never resolve them to the wrong account.

    `all_brands` is the full brands.yaml `brands` mapping. It is used ONLY to
    tell the vault reader which brand keys/aliases belong to OTHER brands, so a
    note tagged for one brand can never be rendered into another brand's brief.
    Optional so existing callers and tests keep working; when omitted the vault
    falls back to frontmatter-only ownership, which is safe but less precise.
    """
    plat_lines, routing = _routing_summary(brand, defaults)
    footer = (brand.get("caption_footer") or "").strip()
    footer_ar = (brand.get("caption_footer_ar") or "").strip()

    design_note = ""
    rules = brand.get("design_rules")
    if rules:
        # The `interior-design` skill and the quartz-vs-ceramic example are Delta
        # Cuisine's (kitchen/wardrobe material law). Ceram Quartz has design_rules
        # too now, but pointing its runs at a skill written for another brand's
        # furniture would be a brand-isolation leak of exactly the kind the vault
        # reader guards against — so the checklist line is rendered only for the
        # brand whose rules it actually describes.
        has_interior_rules = any(k in rules for k in ("kitchen", "wardrobe"))
        checklist_line = (
            "Every kitchen/wardrobe concept and render MUST obey these. Run the "
            "`interior-design` skill's pre-generation checklist before generating.\n"
            if has_interior_rules
            else "Every concept, render and slide MUST obey these.\n"
        )
        example_line = (
            "NEVER ask me 'quartz or ceramic?' or 'which texture?' — "
            if has_interior_rules
            else "NEVER ask me 'which slab?' or 'which texture?' — "
        )
        design_note = (
            "=== DESIGN RULES — HARD CONSTRAINTS (from brands.yaml) ===\n"
            + checklist_line
            + "YOU CHOOSE THE MATERIALS. You have everything you need to decide: the "
            "swatch library in texture_folder, the harmony laws below, the brand's "
            "conception references, and this brand's past posts. Pick the material "
            "family and the exact swatch name yourself, STATE the pick and a one-line "
            "reason in the plan, and continue. " + example_line + "deciding that, "
            "on-brand, is your job. I correct a pick at plan review if I want "
            "something else.\n" + _render_design_rules(rules) + "\n\n"
        )

    psd_note = ""
    master_psd = brand.get("master_psd_template")
    if master_psd:
        psd_note = (
            "=== MASTER PSD TEMPLATE ===\n"
            f"This brand's carousel slides are built from `{master_psd}` (in this "
            "working folder) — a real, layered Photoshop template whose layer "
            "groups toggle between slide looks. Use the `photoshop` MCP tools "
            "(photoshop_get_document_info, photoshop_get_layers, "
            "photoshop_set_layer_visibility, photoshop_update_text_content, "
            "photoshop_place_image, photoshop_save_document, etc.) to drive real "
            "Photoshop against this file for every carousel item: open it, toggle "
            "the layer groups for the slide type you're building, edit the "
            "product-name text layer, place the generated Magnific image(s), then "
            "save the result as a NEW file (never overwrite the master template) "
            "and export/flatten to PNG for posting. Read the brand charter's "
            "carousel-structure section for exactly which layer groups correspond "
            "to which slide before touching anything.\n\n"
        )

    # Rendered only for the lanes that actually call Magnific. publish reuses an
    # already-approved file and ads reuses organic creative — neither generates,
    # and neither carries mcp__magnific in its allowlist, so a cost/flow contract
    # there would be dead text in an already long brief.
    magnific_note = ""
    if task_type in ("weekly", "campaign", "rush", "revise", "approve", "adcreative"):
        magnific_note = _magnific_note(brand)

    # --- The two accumulating knowledge sources -----------------------------
    # Everything above this point is static configuration. These two blocks are
    # what make the CM improve run over run instead of restarting from zero:
    # the vault carries strategy the user WROTE, the lessons carry preferences
    # Era INFERRED from corrections the user gave on earlier runs.
    #
    # Both are best-effort by design. A missing vault or an unreadable lesson
    # store degrades the brief's quality; it must never fail the job, because a
    # CM run that could have produced good work should not die over a note.
    try:
        other_brands = {
            key: (cfg_brand.get("aliases") or [])
            for key, cfg_brand in (all_brands or {}).items()
            if key != brand_key
        }
        vault_note = cm_vault.render_vault_context(
            brand_key,
            brand.get("aliases") or [],
            task_type,
            other_brands=other_brands or None,
        )
    except Exception as exc:  # noqa: BLE001 - never block a run on context
        log_event(
            dispatch_logger, "cm.vault.render_failed", "WARNING", brand=brand_key, error=str(exc)
        )
        vault_note = ""

    try:
        lessons_note = cm_learning.render_lessons(brand_key)
    except Exception as exc:  # noqa: BLE001 - never block a run on context
        log_event(
            dispatch_logger, "cm.learning.render_failed", "WARNING", brand=brand_key, error=str(exc)
        )
        lessons_note = ""

    # Ads is a genuinely different task, not a section of the organic-content
    # brief — see _build_ads_brief's docstring for why (the organic brief's
    # "never stop to ask" instruction directly contradicts the mandatory
    # per-campaign budget question). Early return, skip everything below.
    if task_type == "ads":
        return _build_ads_brief(brand_key, brand, instruction, vault_note, lessons_note)

    offer_note = _offers_note(brand)
    caption_note = _caption_rules_note(brand)
    # Keep the autonomy paragraph coherent for brands that have no offers block —
    # otherwise it points at a "LIVE OFFERS" section that was never rendered.
    offer_clause = (
        "any discount or offer that is NOT listed in LIVE OFFERS above."
        if offer_note
        else "any discount or offer at all — this brand has no live offer, so never invent one."
    )
    reel_to = ", ".join(routing.get("reel", [])) or "(WORKFLOW default)"
    car_to = ", ".join(routing.get("carousel", [])) or "(WORKFLOW default)"
    rush_note = ""
    if task_type == "rush":
        rush_note = (
            "RUSH MODE — a sudden campaign. This is time-sensitive and ALWAYS "
            "available, even while a weekly plan is mid-production. Skip deep "
            "research, go straight to one strong on-brand concept, decide every "
            "creative detail yourself (material, texture, hook, copy), and produce it. "
            'Save it as a DRAFT (plain filename, NO "(approved)") and SHOW IT TO ME. '
            "Speed skips the PLAN gate, never the ASSET gate: do not publish it until "
            "I have seen the finished asset and approved it.\n\n"
        )

    if task_type == "adcreative":
        rush_note = (
            "AD-CREATIVE MODE — build ad creative, stop at a draft.\n"
            "You are producing PAID-PLACEMENT creative: one strong on-brand visual "
            "plus its copy, sized for every placement this brand runs. You are NOT "
            "building or funding a campaign — no ad account, no budget, no audience, "
            "no Meta anything. That is the separate `ads` lane and it needs my budget "
            "decision first; if this order actually asks for spend, say so and stop.\n"
            "Work in this order: (1) pick ONE concept that obeys the DESIGN RULES; "
            "(2) reuse an existing approved asset when one fits — check this brand's "
            "folder before generating anything, because reusing costs 0 credits; "
            "(3) only if nothing fits, generate the hero visual per the MAGNIFIC "
            "block (simulate first); (4) fan it out to the ad ratios with "
            "`design_auto_resize`; (5) write the ad copy yourself.\n"
            'Deliver each variant as a DRAFT — plain filename, NO "(approved)" — with '
            "its caption in a sibling `<base>_description.txt`, then show me every "
            "one. Publish nothing. Attach nothing to a live campaign.\n\n"
        )

    # Gate 2 only means something if the finished asset actually reaches the user.
    # A headless run has no screen, so it must push the draft out over a channel the
    # user reads — hence the explicit hand-off contract below.
    # "approve" is included: approving at Gate 1 produces a fresh draft, which then
    # has to reach the user for Gate 2 exactly like any other produced asset.
    # "adcreative" is included for the same reason and is the whole point of the
    # lane: its output has to land in the SAME review queue as organic drafts, or
    # the existing approve/publish lanes can never pick it up.
    if task_type in ("weekly", "campaign", "rush", "revise", "approve", "adcreative"):
        rush_note += (
            "ASSET REVIEW — how you show me a finished draft (GATE 2).\n"
            "For every asset you finish, tell me, in your final summary: the item name "
            '("reel 1"), its ABSOLUTE file path, the material/swatch you used, and the '
            "caption body you wrote.\n"
            "THEN PUSH IT TO ME ON TELEGRAM so I can review it even when I am away from "
            "the machine. Run this once per finished asset:\n"
            f'        python "{ERA_DIR / "era_telegram.py"}" asset "<absolute path>" '
            '--caption "<item name> — <material/swatch> — awaiting your approval"\n'
            'It prints {"ok":true} on success. If it prints ok:false, say so in your '
            "summary — do not pretend I have seen the asset. Also email it to the notify "
            "address above as a backup.\n"
            "Then say plainly which items are waiting on my approval and that I can "
            'reply "approve reel 1" or name a change.\n'
            'THE APPROVAL INVARIANT: the string "(approved)" in a filename is MY '
            "signature, never yours. Write it ONLY into a file whose finished asset I "
            "have just explicitly approved at GATE 2. A plan approval is NOT an asset "
            "approval. If you produced the asset during this run, it is a draft — it "
            'does not get "(approved)", and it does not get published, no matter how '
            "good it looks or how urgent the order was.\n\n"
        )

    if task_type in ("weekly", "campaign"):
        rush_note += (
            "PLAN-FIRST MODE — TWO separate gates. Approving the PLAN is NOT approving "
            "the ASSET: a render can come out wrong even from a plan I liked.\n"
            "  0. FIRST, look for an existing plan.md for this week/campaign. If one "
            "EXISTS, do NOT rewrite it and do NOT re-plan. Read it, apply whatever my "
            "order below says, and continue from wherever it left off. Only write a "
            "new plan.md when there is genuinely no plan yet.\n"
            "  1. Write the FULL week/campaign plan: every reel and carousel, each "
            "with its slot (day + time from posting_schedule), format, hook, the "
            "MATERIAL YOU CHOSE (family + exact swatch name + one-line why), the "
            "wardrobe reference it follows if applicable, and the caption body. "
            "Decide all of it yourself — a plan with an open question in it is an "
            "unfinished plan.\n"
            "  2. Give every item a `status:` line in plan.md — start them all at "
            "`status: pending`. Then STOP and show me the plan. Produce nothing yet.\n"
            "  ▶ GATE 1 (the plan). I reply per item. Set `status: plan_approved` on "
            "the ones I approved, `status: needs_changes` + my note on the rest. If I "
            'say "approve the plan" with no exceptions, every item becomes '
            "plan_approved.\n"
            "  3. Produce ONE ITEM AT A TIME, `plan_approved` items only. Run your own "
            "charter QC on each finished asset and rework it until it passes.\n"
            "  4. Save each finished asset as a DRAFT — its plain filename, NEVER with "
            '"(approved)" in it — plus its "<base>_description.txt" caption. Set '
            "`status: awaiting_review` in plan.md. Then SHOW IT TO ME (see ASSET REVIEW "
            "below) and move on to producing the next plan_approved item while I look.\n"
            "  ▶ GATE 2 (the asset). Nothing is published until I approve the produced "
            'asset itself. I reply "approve reel 1" (-> you rename it to include '
            '"(approved)", set `status: approved`, and schedule/publish it for its '
            "slot) or I name a correction (-> revise it and show me again).\n"
            "  5. NEVER produce a `pending` or `needs_changes` item. NEVER publish "
            'anything the plan did not list. NEVER write "(approved)" into a filename '
            "yourself — that string is MY approval and only I grant it.\n"
            "  6. When every item is either published or awaiting_review, STOP and give "
            "me the list of what is waiting on me.\n\n"
        )

    # Rendered for revise too: a mixed order ("approve reels 2 and 3, change reel 1")
    # routes to revise, whose brief defers approvals to these rules — so they must be
    # present in the same brief, not cross-referenced from a block that isn't there.
    if task_type in ("approve", "revise"):
        rush_note += (
            "APPROVE MODE — I am approving something. Work out WHICH gate from plan.md.\n"
            '  1. Read plan.md and find the item(s) I named. If I said "approve the '
            'plan" (or "approve everything") with no item named, it applies to every '
            "item not already past that gate.\n"
            "  2. For each named item, look at its `status`:\n"
            "     • `pending` or `needs_changes`  -> this is GATE 1. Set "
            "`status: plan_approved`, then produce it per PLAN-FIRST steps 3-4: make "
            'the asset, QC it, save it as a DRAFT (no "(approved)" in the filename), '
            "set `status: awaiting_review`, and show it to me. Do NOT publish it.\n"
            "     • `awaiting_review`             -> this is GATE 2. The asset is what I "
            'approved. Rename the file to include "(approved)", keep its '
            '"<base>_description.txt" caption beside it, set `status: approved`, then '
            "schedule or publish it for its slot per PUBLISH MODE's hosting + Brand "
            'Lock rules, and write a ".posted" marker on success.\n'
            "     • `approved` or already posted   -> say so and change nothing.\n"
            "  3. Touch NOTHING I did not name. Never re-plan the week. End with one "
            "line per item saying which gate it passed and what happens next.\n\n"
        )

    if task_type == "revise":
        rush_note += (
            "REVISE MODE — I am correcting specific items, not restarting the run.\n"
            "  1. Read the current plan.md, find exactly the item(s) I named (e.g. "
            '"reel 1", "the wardrobe carousel"). NEVER re-plan the week.\n'
            "  2. Apply ONLY the change I asked for (a different texture/material, a "
            "rewritten description, a new hook) to the item(s) I named. Leave every "
            "other item untouched — EXCEPT that if my order ALSO approves other items "
            '("approve reels 2 and 3, change reel 1"), handle those exactly as '
            "APPROVE MODE describes, by their current `status`.\n"
            "  3. If the item is still `pending`/`needs_changes`, the change belongs in "
            "the PLAN: edit the plan entry, set `status: plan_approved` only if I also "
            "approved it, and show me the revised plan entry.\n"
            "  4. If the item was already PRODUCED (`awaiting_review`), supersede the "
            'old draft (never delete anything carrying ".posted"), regenerate it with '
            "my change, re-run the design-rule + caption-rule checks, and save it again "
            'as a DRAFT — plain filename, NO "(approved)" — with `status: '
            "awaiting_review`. Then SHOW IT TO ME again.\n"
            "  5. A revised asset is NOT approved just because I asked for the change. "
            "It goes back through GATE 2. Never publish it, and never write "
            '"(approved)" into its filename yourself.\n'
            "  6. Report what changed in one line. Do not re-plan the week.\n\n"
        )

    # SELF-CRITIQUE GATE — the quality bar. The brief above tells the run to
    # produce; this makes it hold every visual to a professional standard BEFORE
    # showing the user, and re-render what fails, instead of shipping a
    # compliant-but-flat first draft. It is embedded verbatim (not just referenced)
    # because the headless brand config may not have the reel-art-director skill
    # loaded, and a checklist that isn't in the prompt is a checklist the run can't
    # apply. Rendered only for the lanes that actually generate a visual — never
    # for publish (already-approved assets) or ads (reuses organic creative).
    qc_note = ""
    if task_type in ("weekly", "campaign", "rush", "revise", "approve", "adcreative"):
        qc_note = (
            "SELF-CRITIQUE GATE - the deliverable is a POST-READY asset, not a first "
            "draft. Before you show me ANY reel or image, you MUST critique your own "
            "output and fix what fails. Apply the `reel-art-director` skill's bar. "
            "Download and actually VIEW the generated stills and the combined video "
            '(never trust a "completed" status), then for EACH asset score these six:\n'
            "  1. Does it obey every brand hard rule (materials, module/facade, bedding, "
            "the kitchen faucet rule)? Check the brand's design rules.\n"
            "  2. Does it look LIVED-IN and premium, or empty/sterile? Empty FAILS — "
            "re-prompt with real contents (a real couple's clothes, styled props, "
            "greenery, implied human warmth — never bare rails or clear countertops).\n"
            "  3. Is every shot a genuinely different type, or do two read the same? "
            "Near-duplicates FAIL — re-roll one as a different shot type.\n"
            "  4. Is the lighting believable, are verticals straight, any CGI tells? "
            "Flat even lighting, bent verticals or blown highlights FAIL.\n"
            "  5. Does the reel's rhythm alternate wide<->close and flow as one piece?\n"
            "  6. Would a real furniture brand actually post this? If you hesitate, it is "
            "NOT ready.\n"
            "Anything that fails 1-6 gets a SELECTIVE re-render (regenerate only the weak "
            "shot, not the whole set), up to TWO re-rolls per shot. If it still fails "
            "after two, show me anyway and say PLAINLY in one line what is still wrong - "
            "never quietly ship a flat result, and never hide that you were not happy "
            "with it. In your summary, list the self-critique verdict per asset (pass, or "
            "what you re-rolled and why) so I can see you actually looked.\n\n"
        )

    publish_note = ""
    # APPROVE runs can reach Gate 2 and post the asset they just approved, so they
    # need the same hosting/Brand-Lock steps. Rendered for both lanes rather than
    # cross-referenced, because a brief that points at a section it doesn't contain
    # is a brief the headless run cannot follow.
    if task_type in ("publish", "approve"):
        publish_note = (
            (
                "PUBLISH MODE — you are PUBLISHING already-approved assets, not "
                'planning or producing. A filename containing "(approved)" IS the '
                "user's Gate-2 approval; do NOT ask for approval again. Steps:\n"
                if task_type == "publish"
                else "PUBLISHING STEPS — apply these ONLY to an item I just approved at "
                "GATE 2 (its asset existed and was `awaiting_review`). Never publish an "
                "item you produced during this same run.\n"
            )
            + "  1. Find every asset under this brand's folders whose filename contains "
            '"(approved)" and has NO sibling ".posted" marker. If there are none, '
            "say so and STOP — produce nothing.\n"
            '  2. For each, use its companion "<base>_description.txt" caption if one '
            "exists; otherwise WRITE one now — a French, benefit-led body in the brand "
            "voice for the product shown, then append the caption footer above VERBATIM "
            '— and save it as "<base>_description.txt" (UTF-8).\n'
            "  3. HOST THE MEDIA FIRST (local files are NOT publishable directly). "
            "Instagram/Facebook fetch video from a PUBLIC HTTPS URL — they cannot read "
            "a local path, and there is no tool here to upload raw local bytes into "
            "Composio storage (do NOT pass a local path or a made-up s3key; both fail). "
            "So for EACH asset, run this first and read the URL from its JSON stdout:\n"
            f'        python "{ERA_DIR / "media_host.py"}" "<absolute local path>" --json\n'
            '     It prints {"ok":true,"url":"https://..."}. Use that `url` as the '
            "video_url (reels/video) or image_url (carousel images) in the post calls. "
            "If it returns ok:false, STOP and report the reason — do not try to post a "
            "local path.\n"
            "  4. Post it to THIS brand's Instagram AND Facebook using ONLY the exact "
            "Composio ids in the Brand Lock above — never Composio's default account "
            "(the default IG account belongs to a DIFFERENT brand; omitting the id "
            "posts to the wrong page). Instagram is a TWO-STEP flow: create the media "
            "container (INSTAGRAM_POST_IG_USER_MEDIA) with media_type=REELS and the "
            "hosted video_url, then publish it (INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH), "
            "passing the numeric ig_user_id from the Brand Lock. Same caption on both "
            "IG and FB. (Carousels: IG + FB only, never TikTok.)\n"
            "     CAPTION ENCODING — pass the caption/message to the tool call as PLAIN "
            "UTF-8 TEXT, verbatim, exactly as written (accents, apostrophes, #, +, em "
            "dashes, emoji included). NEVER run it through quote()/urlencode() or any "
            "percent-encoding yourself first — the tool call is not a URL you are "
            "building by hand, and the Graph API/Composio layer already encodes the "
            "request correctly. Pre-encoding it yourself is what has previously posted "
            'literal garbage like "d%27exception" and "%23CeramQuartz" as the actual '
            "live caption text on Ceram Quartz's page. After a confirmed post, if you "
            "can, check the returned/live caption for stray % sequences and treat that "
            "as a failed post needing a fix, not a cosmetic detail.\n"
            '  5. After a CONFIRMED success, write a ".posted" marker beside the file '
            "so it can never be double-posted, then email the notify address a one-line "
            "report of what went where.\n"
            "  6. Clean up the intermediate host now that every platform in step 4 has "
            "confirmed it received the media — run:\n"
            f'        python "{ERA_DIR / "media_host.py"}" --cleanup "<the url from step 3>" --json\n'
            "     The staged copy is hosted on Cloudinary and really is deleted. An "
            "asset that was already gone reports success too, so this is safe to "
            "always call, and it never blocks the report in step 5.\n\n"
        )
    return (
        f'You are the senior community manager for "{brand.get("display_name")}" '
        f"(brand key: {brand_key}).\n\n"
        f"Follow WORKFLOW.md (in the added Era directory) and this brand's charter, "
        f"which lives IN THE VAULT and must be read from there:\n"
        f"    {cm_vault.charter_path(brand_key)}\n"
        f"The vault is the single source of truth for this brand. Any copy of the "
        f"charter still sitting in a project folder is stale — do not read it.\n\n"
        f"=== BRAND LOCK — the ONLY accounts you may touch for this brand ===\n"
        f"{plat_lines}\n"
        f"    reels -> {reel_to}\n"
        f"    carousels -> {car_to}\n"
        f"    notify: {brand.get('notify_email', '')}\n"
        f"NEVER post to any account/page/handle not listed above. State this Brand "
        f"Lock back to me before producing or publishing.\n\n"
        + (
            # Bilingual brand: the footer MUST match the description language.
            f"Caption footers — choose the one that MATCHES the language you wrote "
            f"the description body in (never mix):\n"
            f"  • If the description body is in FRENCH, append this VERBATIM:\n{footer}\n"
            f"  • If the description body is in ARABIC (Tunisian dialect), append this "
            f"VERBATIM instead:\n{footer_ar}\n\n"
            if footer_ar
            else f"Caption footer to append:\n{footer}\n\n"
        )
        + f"{rush_note}"
        + f"{publish_note}"
        + f"{offer_note}"
        + f"{caption_note}"
        + f"{design_note}"
        + f"{psd_note}"
        # After design_note on purpose — the Magnific block tells the run that the
        # DESIGN RULES *above* are the only brand authority, so they have to be
        # stated first for that sentence to point at anything.
        + f"{magnific_note}"
        + f"{qc_note}"
        + f"{vault_note}"
        + f"{lessons_note}"
        + "CREDIT DISCIPLINE: generation credits are spent one item at a time, only "
        "on items the approved plan lists. Never mass-generate the whole week in one "
        "pass, and never generate a `pending` or `needs_changes` item.\n\n"
        # The CM is a senior professional: it decides the creative work — material,
        # texture, hook, composition, copy — and states every pick. The ONE human
        # gate is the plan (per-item approve / correct). Publishing is still fenced
        # by the Brand Lock ids, the "(approved)" filename, and the .posted marker.
         + "DECIDE, DO NOT INTERROGATE. You are a senior community manager, not an "
        "order-taker. Every creative choice is yours: material family and exact "
        "swatch, texture, layout reference, hook line, composition, slide order, "
        "prop styling, caption copy. Make the single most on-brand call, STATE it in "
        "the plan with a one-line reason, and keep going. NEVER stop mid-run to ask "
        "me which texture, which material, or which of two options you should use — "
        "that is the work I hired you to do. The plan is where I review your choices, "
        'and a correction from me ("change reel 1\'s texture") is how I steer.\n'
        "The ONLY things you still stop for: (a) the plan itself, before producing; "
        "(b) anything that would post outside this Brand Lock; (c) a price or a "
        f"contractual claim I did not give you; (d) {offer_clause} If you genuinely "
        "cannot proceed, produce your best on-brand version anyway and flag the "
        "assumption in one line — do not return an empty run asking a question.\n\n"
        f"TASK TYPE: {task_type}\n"
        f"ORDER FROM USER: {instruction}\n\n"
        + (
            # Publish runs are the explicit approval — (approved) filenames carry the
            # plan approval, so telling them "do NOT publish" would deadlock the point.
            "Publish the (approved) assets per PUBLISH MODE above, then end with a "
            "short status summary I can read aloud (what posted, where, and the ids used)."
            if task_type == "publish"
            else "Revise only the item I named, save it as an unapproved DRAFT, show it to "
            "me again, and end with a one-line summary of what changed."
            if task_type == "revise"
            else "Apply the approval at whichever gate each named item is at, then end with "
            "one line per item that I can read aloud."
            if task_type == "approve"
            # No PLAN gate here: ad creative is a single concept, not a week's slate,
            # so a per-item plan review would be a gate over one item. The ASSET gate
            # still applies in full — that is where I actually see it.
            else "Produce the ad creative as unapproved DRAFTS, one concept fanned out "
            "to the ad ratios, and show me each variant. End with a short status "
            "summary I can read aloud, including the credits this run actually spent."
            if task_type == "adcreative"
            else "Deliver the complete plan and STOP there for my per-item approval. Then "
            "produce the plan-approved items one at a time as unapproved DRAFTS and "
            "show me each one. Publish nothing until I approve the finished asset. End "
            "with a short status summary I can read aloud."
        )
    )


# Headless `claude -p` runs under --permission-mode acceptEdits: file edits
# (Read/Write/Edit) are auto-approved, but MCP tool calls are NOT — and a headless
# run can't show a permission prompt, so an unlisted MCP call is silently DENIED.
# That means a scheduled auto-publish would create the post request and then fail
# the moment it tries to actually post through Composio, with no error you'd see.
#
# So PUBLISH runs get an explicit allowlist that pre-approves exactly what posting
# needs: find approved files (Read/Glob/Grep), post + Gmail-notify via Composio,
# write .posted markers (Write/Bash). Verified 2026-07-05: a read-only publish run
# with this allowlist reached Composio and confirmed @ceramquartz / page
# 788466647689882.
#
# PRODUCE lanes (weekly/campaign/rush) hit the SAME wall the moment they try to
# GENERATE: image/video creation goes through the Magnific MCP, and — exactly like
# Composio above — an unlisted MCP call is silently DENIED headless. Leaving these
# runs with no allowlist (the previous default) is why the dispatch lane never
# actually produced a visual: it could plan and write text, but every Magnific call
# was dropped with no error. So produce lanes now get a generous allowlist that
# pre-approves the standard toolset PLUS both MCP servers. It's broad on purpose —
# the risk with pinning is denying a tool the run needs, so we list every built-in
# a produce run realistically uses rather than a minimal set. An explicit caller
# allowed_tools always overrides this (e.g. the read-only wiring test).
_PRODUCE_ALLOWED_TOOLS = (
    "Read,Write,Edit,Glob,Grep,Bash,WebSearch,WebFetch,TodoWrite,Task,"
    "mcp__magnific,mcp__composio,mcp__context7"
)
_DEFAULT_ALLOWED_TOOLS = {
    "publish": "Read,Glob,Grep,Write,Bash,mcp__composio",
    "weekly": _PRODUCE_ALLOWED_TOOLS,
    "campaign": _PRODUCE_ALLOWED_TOOLS,
    "rush": _PRODUCE_ALLOWED_TOOLS,
    # revise regenerates one asset as a draft, so it needs the producing surface.
    "revise": _PRODUCE_ALLOWED_TOOLS,
    # adcreative generates ad-format creative (Magnific flows + design_auto_resize)
    # and stops at a DRAFT. Deliberately a separate lane from `ads` rather than a
    # loosening of it: `ads` must stay unable to generate (see its entry below), so
    # that spending money and making pictures remain two different orders.
    "adcreative": _PRODUCE_ALLOWED_TOOLS,
    # approve straddles both gates: at Gate 1 it produces a draft, at Gate 2 it hosts
    # and posts. _PRODUCE_ALLOWED_TOOLS already carries mcp__composio, so it covers both.
    "approve": _PRODUCE_ALLOWED_TOOLS,
    # Ads needs the facebook-ads MCP (create/manage campaigns, always paused) and
    # composio (emailing the performance-report PDF per era-ads-manager's Step 6)
    # — deliberately NOT mcp__magnific, ads reuses already-produced organic assets
    # rather than generating new creative.
    # Paid analysis plugins (Adspirer, HYPD) are deliberately NOT listed: Karim
    # won't pay for them (2026-09-25). Analysis runs on the free facebook-ads
    # connector through the in-house era-meta-analyst skill; unlisted servers are
    # silently denied headless, so a paid plugin can never burn a trial or quota.
    "ads": "Read,Write,Edit,Glob,Grep,Bash,mcp__facebook-ads,mcp__composio",
}


# ---------------------------------------------------------------------------
# Ads runs that fail (or would fail) silently — added 2026-09-20
#
# "How are my campaigns doing" produced NO answer. The investigation found three
# separate causes, none of them a bad brief:
#   1. Meta's OAuth token for the facebook-ads MCP had expired (18 Sep) and the
#      stored entry has no refresh token, so it can never renew itself. Every
#      run reached Meta and got nothing back — a 20-minute, $10.84 run that
#      produced a report full of "[NOT PULLED]".
#   2. The order waited 11–40 minutes behind weekly content jobs in the one-job-
#      at-a-time queue, though a read-only report touches none of the shared
#      resources (Magnific, Adobe) that queue exists to protect.
#   3. The result finished while Era was in standby and was fed into a muted
#      session (see cm_flow._deliver_or_queue_cm_report).
# ---------------------------------------------------------------------------

# A backstop against a runaway loop, NOT a real spend guard. Two facts make it a
# ceiling rather than a target: (1) the real wall-clock cap is `timeout`; (2) this
# account routes through Omni-Route, which serves `auto/best-coding` via free
# models but Claude Code still PRICES it as a premium first-party model — so the
# "cost" this cap sees is phantom (the 2026-09-20 report hit $4 of fictional spend
# at turn 8 and was killed mid-report). Set high enough that a lean report never
# trips it, low enough to stop an infinite loop; if Era is ever pointed at a real
# paid route, it is also a sane per-report ceiling.
_ADS_OPS_MAX_BUDGET_USD = 15

# In-flight read-only ads reports, keyed (brand, normalised order). Asking twice
# used to queue two full paid runs; the second ask now joins the first.
_READONLY_INFLIGHT: set = set()
_READONLY_INFLIGHT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Ads runs go DIRECT on the freelance account's Claude subscription — 2026-09-25
#
# Through OmniRoute an ads run had no prompt caching (a 27-turn build re-sent
# ~4M tokens: 15 minutes), its free models ignored "don't ask again", and under
# load the gateway answered "503 Chat admission capacity unavailable". Karim
# re-logged the freelance account into its subscription and asked for ads to go
# direct. The gateway stays as the automatic fallback: if the subscription is
# signed out, rate-limited or out of usage, the same run retries through it, so
# an ads job never simply stops.
# ---------------------------------------------------------------------------
_DIRECT_MODEL = os.getenv("ERA_CLAUDE_MODEL", os.getenv("ERA_ADS_MODEL", "sonnet"))
_ADS_DIRECT_MODEL = _DIRECT_MODEL  # back-compat name
# User-level env vars that point Claude Code at OmniRoute (API key + base URL +
# gateway model alias). With them removed, `claude -p` uses the account's own
# subscription login stored in CLAUDE_CONFIG_DIR.
_GATEWAY_ENV_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)
# A direct run that fails with any of these never reached real work — the route
# itself is unavailable, so the gateway gets the same run.
_ROUTE_FAILURE_MARKERS = (
    "oauth session expired",
    "failed to authenticate",
    "not logged in",
    "/login",
    "invalid api key",
    "usage limit",
    "limit reached",
    "rate limit",
    "rate_limit",
    "overloaded",
    "429",
    "529",
    "credit balance",
)


# Which MCP servers a lane cannot work without — a second Claude profile is only a
# useful failover for a lane if it has them (ads are checked via their OAuth token).
_PRODUCE_SERVERS = ("magnific", "composio")
_LANE_SERVERS = {
    "publish": ("composio",),
    "weekly": _PRODUCE_SERVERS,
    "campaign": _PRODUCE_SERVERS,
    "rush": _PRODUCE_SERVERS,
    "revise": _PRODUCE_SERVERS,
    "approve": _PRODUCE_SERVERS,
    "adcreative": _PRODUCE_SERVERS,
}


def _claude_accounts(cfg):
    """Every Claude profile Era may run under (freelance, delta, ...), in
    brands.yaml order, de-duplicated."""
    dirs = []
    candidates = [(cfg.get("defaults") or {}).get("ads_claude_config_dir")]
    candidates += [b.get("claude_config_dir") for b in (cfg.get("brands") or {}).values()]
    for d in candidates:
        if d:
            d = os.path.expandvars(d)
            if d not in dirs:
                dirs.append(d)
    return dirs


def _account_can_run(config_dir, task_type):
    """Can this Claude profile do this lane's work at all? Ads need the
    facebook-ads token; produce/publish lanes need their MCP servers registered.
    A profile that fails this is never used as a failover — running a Meta job
    on a profile with no Meta connection would 'succeed' with nothing done."""
    if task_type == "ads":
        return _ads_connection_problem(config_dir) is None
    needed = _LANE_SERVERS.get(task_type, ())
    if not needed:
        return True
    try:
        data = json.loads((Path(config_dir) / ".claude.json").read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
    except Exception:  # noqa: BLE001 - unreadable profile is not a usable failover
        return False
    return all(s in servers for s in needed)


def _routes_for(accounts, env):
    """(name, env, extra_args, config_dir) in the order to try.

    Karim, 2026-09-25: "use both claude profiles for Era — if one fails use the
    other and vice versa". Each profile's own subscription first (direct, cached,
    fast), then OmniRoute on the first profile as the last resort.
    """
    routes = []
    for d in accounts:
        direct_env = {k: v for k, v in env.items() if k not in _GATEWAY_ENV_VARS}
        direct_env["CLAUDE_CONFIG_DIR"] = d
        routes.append((f"direct:{Path(d).name}", direct_env, ["--model", _DIRECT_MODEL], d))
    gateway_env = {**env, "CLAUDE_CONFIG_DIR": accounts[0]}
    routes.append((f"gateway:{Path(accounts[0]).name}", gateway_env, [], accounts[0]))
    return routes


def _is_route_failure(proc):
    if proc.returncode == 0:
        try:
            if not json.loads(proc.stdout).get("is_error"):
                return False
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    return any(m in text for m in _ROUTE_FAILURE_MARKERS)


def _run_on_routes(routes, run_cmd, run_input, project_dir, timeout, brand_key):
    """Run one headless attempt, falling through `routes` while the route itself
    (login, usage limit, rate limit) is what failed. Returns (proc, config_dir
    that produced it)."""
    proc, used_dir = None, None
    for j, (name, run_env, extra, config_dir) in enumerate(routes):
        if name.startswith("gateway"):
            _ensure_gateway(run_env)
        proc = subprocess.run(
            run_cmd + extra,
            input=run_input,
            cwd=project_dir,
            env=run_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        used_dir = config_dir
        if j + 1 < len(routes) and _is_route_failure(proc):
            log_event(
                dispatch_logger,
                "cm.route.fallback",
                "WARNING",
                brand=brand_key,
                route=name,
                detail=(proc.stdout or proc.stderr or "")[-300:],
            )
            continue
        if len(routes) > 1:
            log_event(dispatch_logger, "cm.route.used", "INFO", brand=brand_key, route=name)
        return proc, used_dir
    return proc, used_dir


def _ensure_gateway(env):
    """Start OmniRoute if the run is pointed at it and it is down — Era must not
    depend on ZORO's launcher having started it (2026-09-25). Never raises."""
    try:
        from core import omniroute

        ok, detail = omniroute.ensure_running(env=env)
        if not ok or detail.startswith("started"):
            log_event(
                dispatch_logger,
                "cm.gateway.ensure",
                "INFO" if ok else "WARNING",
                ok=ok,
                detail=detail,
            )
    except Exception:  # noqa: BLE001 - a gateway check never blocks a job
        pass


# ---------------------------------------------------------------------------
# Ads build follow-ups continue the SAME session — added 2026-09-25
#
# "Launch meta ads for the last story, 14 days, $3-4/day" asked for the budget;
# the answer ("end 2026-10-09, $3/day") arrived as a NEW order in a NEW headless
# session. That session had never seen "the last story", re-read the whole vault
# for 15 minutes (27 turns, ~4M uncached tokens through the gateway), guessed the
# wrong slab (Polaris, not Taj Mahal) and asked three new questions. Every answer
# restarted the loop, so nothing was ever built. A build order that follows a
# recent build run for the same brand now resumes that session (`--resume`).
# ---------------------------------------------------------------------------
_ADS_SESSIONS_PATH = Path(
    os.getenv("ERA_ADS_SESSIONS_FILE", str(Path.home() / ".era" / "ads_sessions.json"))
)
_ADS_RESUME_WINDOW_S = 3 * 3600


def _load_ads_session(brand_key):
    """The session id of this brand's last ads build run, if recent enough."""
    try:
        data = json.loads(_ADS_SESSIONS_PATH.read_text(encoding="utf-8"))
        entry = data.get(brand_key) or {}
        if time.time() - float(entry.get("ts", 0)) <= _ADS_RESUME_WINDOW_S:
            return entry.get("session_id") or None
    except Exception:  # noqa: BLE001 - no usable session means a fresh brief
        pass
    return None


def _ads_session_account(brand_key):
    """The Claude profile that owns the saved session — a session id only exists
    inside the profile that created it, so a resume must run there."""
    try:
        data = json.loads(_ADS_SESSIONS_PATH.read_text(encoding="utf-8"))
        return (data.get(brand_key) or {}).get("config_dir")
    except Exception:  # noqa: BLE001
        return None


def _save_ads_session(brand_key, session_id, config_dir=None):
    try:
        try:
            data = json.loads(_ADS_SESSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        data[brand_key] = {"session_id": session_id, "ts": time.time(), "config_dir": config_dir}
        _ADS_SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ADS_SESSIONS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - bookkeeping never fails a run
        pass


def _ads_followup_prompt(instruction):
    return (
        "FOLLOW-UP FROM KARIM on the campaign we were just discussing. Every rule "
        "from the original brief still applies (Ad Account Lock, always PAUSED, "
        "no automated greeting).\n\n"
        f"HIS MESSAGE: {instruction}\n\n"
        "If this answers what you asked, do NOT ask again — build the campaign "
        "now, PAUSED, and end with the short spoken summary. If it is clearly a "
        "different campaign, treat it as a new build under the same rules."
    )


def _ads_mode(instruction):
    """build / report / manage for an ads order. Falls back to "build" — the
    always-paused, always-ask mode — if the detector is unavailable, never to a
    mode that may change live spend."""
    try:
        from app.cm_intents import ads_mode

        return ads_mode(instruction)
    except Exception:  # noqa: BLE001 - degrade to the safe mode
        return "build"


def _ads_connection_problem(config_dir):
    """A plain-language reason the Meta Ads MCP cannot work, or None.

    Reads only the token's expiry and whether a refresh token exists — never the
    token itself. Blocks only on a dead-end (expired AND unable to renew, or no
    entry at all): an expired access token WITH a refresh token is renewed by
    Claude Code on its own, so blocking there would create the outage it guards
    against. Any doubt returns None — a check that can wrongly stop a run costs
    more than the wasted run it prevents (degrade, never raise).
    """
    try:
        creds = json.loads((Path(config_dir) / ".credentials.json").read_text(encoding="utf-8-sig"))
        entries = creds.get("mcpOAuth") or {}
        entry = next(
            (
                v
                for k, v in entries.items()
                if k.startswith("facebook-ads|") or v.get("serverName") == "facebook-ads"
            ),
            None,
        )
        if entry is None:
            return (
                "Meta Ads isn't signed in on Era's ads account. Authorize the "
                "facebook-ads connector once: run claude under that account, type /mcp, "
                "choose facebook-ads, Authenticate."
            )
        expires_ms = entry.get("expiresAt")
        if expires_ms and expires_ms / 1000 < time.time() and not entry.get("refreshToken"):
            day = time.strftime("%d %b", time.localtime(expires_ms / 1000))
            return (
                f"Meta Ads' sign-in expired on {day} and can't renew itself. Nothing was "
                "changed. Re-authorize the facebook-ads connector once: run claude under "
                "Era's ads account, type /mcp, choose facebook-ads, Authenticate."
            )
    except Exception:  # noqa: BLE001 - unreadable/absent state means "can't judge"
        return None
    return None


def _explain_run_failure(raw):
    """Turn a headless run's raw failure text into a sentence Era can say.

    The raw text is a JSON blob or a bracketed CLI code; read aloud, it told Karim
    nothing about what to do. The full text stays in the run log and the vault.
    """
    text = str(raw or "")
    low = text.lower()
    # Order matters — the specific, actionable causes come first, and the ones
    # that co-occur as NOISE come last. A budget-exhausted run also emits an
    # `unrecognized_model` line on stderr (a secondary SDK call), so budget must
    # be judged before that line can win. "Connection refused" is the gateway
    # being down; it is the single most common real failure here.
    if "connection refused" in low or "connectionrefused" in low:
        return (
            "Era's model gateway (Omni-Route) refused the connection. It's down or "
            "restarting — start it and ask again. Nothing was changed."
        )
    if "oauth session expired" in low or "not logged in" in low or "/login" in low:
        return (
            "The Claude account Era uses for this job is signed out. Run claude once "
            "under that account and log in, then ask again."
        )
    if "budget" in low and any(
        m in low for m in ("reached maximum", "exhausted", "exceeded", "max_budget")
    ):
        return (
            "That report ran long and stopped at its work limit before finishing — "
            "usually the model gateway not caching, so it re-reads everything each step. "
            "Nothing was changed; ask for a narrower report (one campaign, headline numbers)."
        )
    if any(m in low for m in ("rate limit", "rate_limit", "429", "overloaded", "credit balance")):
        return "The model provider is rate-limiting or out of credit right now. Try again shortly."
    if "unrecognized_model" in low:
        return (
            "The model Era was told to use isn't recognised — ANTHROPIC_MODEL or the "
            "gateway is pointing at a route that isn't available right now."
        )
    return text.strip()[:300] or "unknown error"


def _log_to_vault(brand_key, task_type, instruction, outcome, detail=""):
    """Append this run to the brand's Obsidian run-log note. Best effort.

    Kept as a wrapper rather than calling cm_vault directly at each return so
    that a vault problem (OneDrive syncing, path moved, disk full) can never
    convert a finished CM run into a failure. The run already happened; the
    log is bookkeeping.
    """
    try:
        cm_vault.append_run_log(brand_key, task_type, instruction, outcome, detail)
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a run
        log_event(
            dispatch_logger, "cm.vault.log_failed", "WARNING", brand=brand_key, error=str(exc)
        )

    # ...and into Era's own dated log, in Era's own vault.
    #
    # Two different questions, two different notes. The run log above answers
    # "what has Era done for this brand" and lives with the brand. This one
    # answers "what happened today", across every brand — which no per-brand log
    # can, and which is exactly what gets lost between sessions.
    #
    # Separately wrapped, not folded into the try above: a daily-log failure must
    # not cost the brand log that already succeeded.
    try:
        from core import daily_note

        daily_note.append_session(
            f"{brand_key.replace('_', ' ').title()} — {task_type}",
            done=[f"`{task_type}` run finished: **{outcome}**. Order: {instruction.strip()[:200]}"],
            in_progress=[detail.strip()[:300]] if detail.strip() else None,
            notes_touched=[f"[[Era_Run_Log_{brand_key}]]"],
        )
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a run
        log_event(
            dispatch_logger, "cm.daily.log_failed", "WARNING", brand=brand_key, error=str(exc)
        )


def run_task(
    instruction,
    task_type="weekly",
    brand_key=None,
    allowed_tools=None,
    extra_args=None,
    timeout=_DEFAULT_TIMEOUT,
    lock_timeout=None,
):
    """Dispatch one order to the right Claude account, one at a time.

    `timeout` caps the headless run itself; `lock_timeout` caps how long we
    wait for a job ahead of us in the queue. They were one value, which meant
    a job could burn the whole budget waiting and still be given a full run.
    """
    if lock_timeout is None:
        lock_timeout = timeout
    cfg = load_brands()
    brand_key = brand_key or resolve_brand(instruction, cfg)
    if not brand_key:
        return {
            "ok": False,
            "need_brand": True,
            "message": "Couldn't tell which brand this is for.",
        }

    # Publish runs need Composio pre-approved or the post is silently denied headless
    # (see _DEFAULT_ALLOWED_TOOLS). An explicit caller allowlist always wins.
    if allowed_tools is None:
        allowed_tools = _DEFAULT_ALLOWED_TOOLS.get(task_type)

    brand = cfg["brands"][brand_key]
    if brand.get("status") not in ("active",):
        return {
            "ok": False,
            "brand": brand_key,
            "error": f'Brand "{brand_key}" is not active yet — finish its brands.yaml setup.',
        }

    # Some brands are served by ONE lane only — Brico Kitchen is boost-only (Karim
    # runs its paid campaigns; another team makes its content). An organic order
    # for it must be refused here, before any brief is built or credit is spent.
    lanes = brand.get("lanes")
    if lanes and task_type not in lanes:
        return {
            "ok": False,
            "brand": brand_key,
            "error": (
                f"{brand.get('display_name', brand_key)} is handled for "
                f"{', '.join(lanes)} only — Karim doesn't make its content. "
                "Nothing was done."
            ),
        }

    # Ads is the one lane with real money on the other side of a mistake, so it
    # gets its own explicit gate rather than inheriting "active" — flipping
    # ads.enabled is a deliberate, separate decision from the brand going live
    # organically (see brands.yaml's ads: block comments for what has to be
    # true first: OAuth done, the correct ad account confirmed, no standing
    # budget cap because budget is asked per campaign instead).
    if task_type == "ads" and not (brand.get("ads") or {}).get("enabled"):
        return {
            "ok": False,
            "brand": brand_key,
            "error": (
                f'Meta Ads is not enabled for "{brand_key}" yet — see '
                f"brands.yaml → {brand_key}.ads and .claude/skills/era-ads-manager/SKILL.md."
            ),
        }

    # Ads tasks ALWAYS run under the freelance/default Claude account, never a
    # brand's own claude_config_dir — the facebook-ads MCP's OAuth token lives
    # in that one account (.claude-freelance), and a brand's own account
    # (e.g. .claude-delta) never had it registered. Every other lane keeps the
    # brand's own account so publishing/producing stays exactly as before.
    if task_type == "ads":
        ads_config_dir = (cfg.get("defaults") or {}).get("ads_claude_config_dir")
        if not ads_config_dir:
            return {
                "ok": False,
                "brand": brand_key,
                "error": "defaults.ads_claude_config_dir is not set in brands.yaml — "
                "ads tasks have nowhere authenticated to run.",
            }
        config_dir = os.path.expandvars(ads_config_dir)
        # Fail in a second, out loud, instead of after a 20-minute paid run that
        # reaches Meta and comes back empty (see _ads_connection_problem) — but
        # only when NO Claude profile has a working Meta connection; otherwise
        # the job moves to the profile that does (2026-09-25).
        problem = _ads_connection_problem(config_dir)
        if problem:
            spare = next(
                (
                    d
                    for d in _claude_accounts(cfg)
                    if d != config_dir and _account_can_run(d, "ads")
                ),
                None,
            )
            if spare:
                log_event(
                    dispatch_logger,
                    "cm.ads.account_switched",
                    "WARNING",
                    brand=brand_key,
                    from_account=Path(config_dir).name,
                    to_account=Path(spare).name,
                    reason=problem[:200],
                )
                config_dir, problem = spare, None
        if problem:
            log_event(
                dispatch_logger,
                "cm.ads.preflight_failed",
                "ERROR",
                brand=brand_key,
                reason=problem,
            )
            _log_to_vault(brand_key, task_type, instruction, "failed", problem[:400])
            return {
                "ok": False,
                "brand": brand_key,
                "error": problem,
                "wrote_files": 0,
                "log_path": None,
            }
    else:
        config_dir = os.path.expandvars(brand["claude_config_dir"])
    project_dir = os.path.expandvars(brand["project_dir"])
    env = {**os.environ, "CLAUDE_CONFIG_DIR": config_dir}
    # This lane's profile first, then every other profile able to do the work.
    accounts = [config_dir] + [
        d for d in _claude_accounts(cfg) if d != config_dir and _account_can_run(d, task_type)
    ]

    # Keep the Magnific MCP usable headless: strip any stale "needs auth" flag so
    # this run's generation tools aren't silently disabled (see the helper).
    for d in accounts:
        _clear_mcp_needs_auth(d)

    # Record the order as feedback BEFORE the run, not after, so the lesson
    # survives even if the run crashes or times out.
    #
    # Two ways in, because the lane alone was too narrow. `revise` and `approve`
    # are judgement BY DEFINITION — a revise IS a correction, an approve IS a
    # confirmation — so they always record.
    #
    # But a preference does not wait for the right lane. "Prepare this week's
    # plan, and stop using the clinical white kitchens" arrives as `weekly`, and
    # under the lane-only gate that standing rule was thrown away. Measured
    # 2026-07-31: **two** lessons had been recorded in the store's entire life,
    # which is why Era kept repeating corrections she had already been given.
    # carries_judgement() asks whether a correction or approval marker actually
    # fired, so a routine "plan the week" still teaches nothing and is skipped.
    if task_type in ("revise", "approve") or cm_learning.carries_judgement(instruction):
        try:
            cm_learning.record_feedback(brand_key, instruction, task_type)
        except Exception as exc:  # noqa: BLE001 - learning must never block work
            log_event(
                dispatch_logger,
                "cm.learning.record_failed",
                "WARNING",
                brand=brand_key,
                error=str(exc),
            )

    # The brief (brand lock + charter + vault notes + lessons) routinely runs to
    # tens of KB. On Windows, CLAUDE_BIN resolves through PATH to the npm-installed
    # `claude.cmd` shim, which subprocess can only launch via `cmd.exe /c` — and
    # cmd.exe's own command-line buffer caps out around 8191 chars. Passing the
    # brief as a positional argv used to blow that limit and surface as
    # `OSError: [WinError 206] The filename or extension is too long`, which had
    # nothing to do with any file path in the order (shortening a video's path
    # never touched the actual oversized argument). Piping the brief over stdin
    # instead — `claude -p` reads the prompt from stdin when no positional
    # argument is given — keeps it off the OS command-line entirely.
    # IMPORTANT: no --bare (that forces an API key). Plain -p uses the subscription
    # session stored in CLAUDE_CONFIG_DIR.
    brief = build_brief(
        brand_key,
        brand,
        task_type,
        instruction,
        defaults=cfg.get("defaults"),
        all_brands=cfg.get("brands"),
    )
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--add-dir",
        str(ERA_DIR),
    ]  # lets the run read WORKFLOW.md + shared skills
    if allowed_tools:  # e.g. "Read,Edit,Write,Bash"
        cmd += ["--allowedTools", allowed_tools]

    # Reading or changing EXISTING campaigns is bounded work with a hard money
    # ceiling. A read-only report also touches none of the shared resources the
    # queue below protects, so it must not wait behind a 30-minute content job.
    ads_mode_now = _ads_mode(instruction) if task_type == "ads" else None
    read_only = ads_mode_now == "report"
    if ads_mode_now in ("report", "manage"):
        cmd += ["--max-budget-usd", str(_ADS_OPS_MAX_BUDGET_USD)]
    if extra_args:
        cmd += list(extra_args)

    # (cmd, stdin) attempts in order: a follow-up resumes the last build session,
    # and falls back to the full brief if that session can't be resumed.
    # Each attempt carries the profiles it may run on.
    attempts = [(cmd, brief, accounts)]
    resume_id = _load_ads_session(brand_key) if ads_mode_now == "build" else None
    if resume_id:
        owner = _ads_session_account(brand_key) or config_dir
        attempts.insert(
            0, (cmd + ["--resume", resume_id], _ads_followup_prompt(instruction), [owner])
        )
    used_dir = config_dir

    # Asking twice used to queue a second full paid run behind the first. A
    # duplicate read-only report joins the one already running instead.
    inflight_key = None
    if read_only:
        inflight_key = (brand_key, " ".join((instruction or "").lower().split()))
        with _READONLY_INFLIGHT_LOCK:
            if inflight_key in _READONLY_INFLIGHT:
                log_event(
                    dispatch_logger,
                    "cm.ads.duplicate_report",
                    "INFO",
                    brand=brand_key,
                    order=instruction[:200],
                )
                return {
                    "ok": True,
                    "duplicate": True,
                    "brand": brand_key,
                    "result": (
                        "That report is already being prepared — I'll give it to you "
                        "the moment it's done."
                    ),
                    "wrote_files": None,
                    "log_path": None,
                }
            _READONLY_INFLIGHT.add(inflight_key)

    # Peek (non-authoritative hint) so Era can tell you what you're queued behind.
    busy = None if read_only else current_job()
    queued_behind = {"brand": busy["brand"], "task_type": busy["task_type"]} if busy else None

    # Serialize everything: shared Magnific + Adobe => strictly one job at a time.
    # (Read-only ads reports are the one exception — see above.)
    run_started = None
    lock = FileLock(
        _LOCK_PATH, timeout=lock_timeout, meta={"brand": brand_key, "task_type": task_type}
    )
    log_event(
        dispatch_logger,
        "cm.dispatch.queued",
        "INFO",
        brand=brand_key,
        task_type=task_type,
        order=instruction[:200],
        queued_behind=queued_behind,
    )
    lock_wait_started = time.time()
    try:
        with (
            contextlib.nullcontext() if read_only else _GLOBAL_LOCK,
            contextlib.nullcontext() if read_only else lock,
        ):
            lock_waited_s = round(time.time() - lock_wait_started, 1)
            # The status file describes THE running serialized job; a concurrent
            # read-only report must neither overwrite it nor clear it on exit.
            if not read_only:
                _write_status(brand_key, task_type, instruction)
            # Stamp the moment OUR job actually begins (not lock-wait time) so the
            # recent-file count credits only files this run touched.
            run_started = time.time()
            log_event(
                dispatch_logger,
                "cm.run.started",
                "INFO",
                brand=brand_key,
                task_type=task_type,
                lock_waited_s=lock_waited_s,
            )
            try:
                # encoding MUST be pinned to utf-8: Claude's headless output is full of
                # é è à ç, em dashes, box-drawing and emoji (📍 ☎ ✅). Without this,
                # text=True decodes with the Windows locale codec (cp1252/charmap),
                # and any non-cp1252 byte (e.g. 0x8d) raises UnicodeDecodeError inside
                # subprocess's reader THREAD — uncaught, it crashed the whole engine.
                # errors="replace" keeps a stray byte from ever taking Era down again.
                for i, (run_cmd, run_input, run_accounts) in enumerate(attempts):
                    proc, used_dir = _run_on_routes(
                        _routes_for(run_accounts, env),
                        run_cmd,
                        run_input,
                        project_dir,
                        timeout,
                        brand_key,
                    )
                    resumed = "--resume" in run_cmd
                    if resumed and proc.returncode != 0 and i + 1 < len(attempts):
                        log_event(
                            dispatch_logger,
                            "cm.ads.resume_failed",
                            "WARNING",
                            brand=brand_key,
                            detail=(proc.stderr or proc.stdout or "")[:300],
                        )
                        continue
                    if resumed:
                        log_event(dispatch_logger, "cm.ads.resumed", "INFO", brand=brand_key)
                    break
            except subprocess.TimeoutExpired:
                log_path = _log_run(
                    brand_key, task_type, instruction, None, "", "TIMEOUT", 0, run_started
                )
                log_event(
                    dispatch_logger,
                    "cm.run.timeout",
                    "ERROR",
                    brand=brand_key,
                    task_type=task_type,
                    timeout_s=timeout,
                    log_path=log_path,
                )
                return {
                    "ok": False,
                    "brand": brand_key,
                    "error": "Timed out.",
                    "wrote_files": 0,
                    "log_path": log_path,
                }
            finally:
                if not read_only:
                    _clear_status()
    except TimeoutError as e:
        # Someone else's job outlasted our patience. Say so precisely — this used
        # to surface as a generic failure 30 minutes after a silent "it's running".
        blocker = current_job()
        log_event(
            dispatch_logger,
            "cm.lock.timeout",
            "ERROR",
            brand=brand_key,
            task_type=task_type,
            waited_s=round(time.time() - lock_wait_started, 1),
            blocker=blocker,
        )
        return {
            "ok": False,
            "brand": brand_key,
            "queued_behind": queued_behind,
            "error": str(e),
            "wrote_files": 0,
            "log_path": None,
        }
    finally:
        if inflight_key is not None:
            with _READONLY_INFLIGHT_LOCK:
                _READONLY_INFLIGHT.discard(inflight_key)

    # How much did this run actually change on disk, and what did it say? Both are
    # captured so a hollow ok=True (0 files) can never masquerade as real work.
    wrote = _count_recent_files(project_dir, run_started)
    log_path = _log_run(
        brand_key,
        task_type,
        instruction,
        proc.returncode,
        proc.stdout,
        proc.stderr,
        wrote,
        run_started,
    )
    log_event(
        dispatch_logger,
        "cm.run.finished",
        "INFO",
        brand=brand_key,
        task_type=task_type,
        returncode=proc.returncode,
        wrote_files=wrote,
        duration_s=round(time.time() - run_started, 1),
        log_path=log_path,
    )

    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout or "").strip()[:2000]
        # The true reason is often in stdout's JSON `result` while stderr holds an
        # unrelated CLI warning (an OAuth expiry hid behind an `unrecognized_model`
        # line this way). Judge the failure on BOTH.
        reason_text = error
        try:
            out = json.loads(proc.stdout)
            if out.get("is_error"):
                # The cause can live in `result`, in `errors`, or only in
                # `terminal_reason`/`subtype` (a budget-capped run has no `result`
                # at all, just terminal_reason="budget_exhausted"). Gather all of
                # them so _explain_run_failure sees the real reason, not stderr noise.
                parts = [
                    str(out.get("result") or ""),
                    " ".join(str(e) for e in (out.get("errors") or [])),
                    str(out.get("terminal_reason") or ""),
                    str(out.get("subtype") or ""),
                    proc.stderr or "",
                ]
                reason_text = "\n".join(p for p in parts if p.strip())
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        friendly = _explain_run_failure(reason_text)
        _log_to_vault(brand_key, task_type, instruction, "failed", friendly[:400])
        return {
            "ok": False,
            "brand": brand_key,
            "queued_behind": queued_behind,
            "error": friendly,
            "raw_error": error,
            "wrote_files": wrote,
            "log_path": log_path,
        }
    try:
        data = json.loads(proc.stdout)
        result = data.get("result", proc.stdout)
        if ads_mode_now == "build" and data.get("session_id"):
            _save_ads_session(brand_key, data["session_id"], used_dir)
    except (json.JSONDecodeError, AttributeError):
        result = proc.stdout

    # A run that wrote nothing is reported honestly in the vault too — a hollow
    # ok=True is exactly the outcome the run log needs to make visible, so the
    # history does not read as a string of successes that produced no assets.
    _log_to_vault(
        brand_key,
        task_type,
        instruction,
        "ok" if wrote else "ok (wrote 0 files)",
        f"{wrote} file(s) changed. {str(result).strip()[:400]}",
    )
    return {
        "ok": True,
        "brand": brand_key,
        "result": result,
        "queued_behind": queued_behind,
        "wrote_files": wrote,
        "log_path": log_path,
    }


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if argv and argv[0] in ("--status", "--unlock", "--force-unlock"):
        if argv[0] == "--status":
            out = {
                "current_job": current_job(),
                "lock_held": Path(str(_LOCK_PATH) + ".lockdir").exists(),
                "lock_owner": FileLock(_LOCK_PATH).owner(),
            }
        else:
            out = unlock(force=argv[0] == "--force-unlock")
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        sys.exit(0)
    order = " ".join(argv) or "prepare this week's content for Delta Cuisine"
    out = run_task(order, task_type="weekly")
    print(json.dumps(out, ensure_ascii=False, indent=2))
