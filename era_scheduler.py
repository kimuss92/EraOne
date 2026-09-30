"""
era_scheduler.py — runs each brand's recurring jobs (weekly plan, auto-publish) at
the times set in brands.yaml, through era_dispatch.run_task — so scheduled jobs use
the SAME global lock as your on-demand voice orders. Nothing ever collides.

This REPLACES brand-specific scheduled tasks living in Claude Desktop. One clock,
one lock, all brands. Era just needs to be running (not Claude Desktop specifically).

REQUIREMENTS
  pip install apscheduler ruamel.yaml

START (once, at Era startup, e.g. top of main.py)
  from era_scheduler import start_scheduler
  start_scheduler()

RESCHEDULE (call this from Era when you ask to change a time — no restart needed)
  from era_scheduler import set_schedule
  set_schedule("delta_cuisine", "weekly_plan", cron="0 10 * * 4", enabled=True)
  # cron format: "minute hour day month day_of_week"  (day_of_week: 0=Mon .. 6=Sun)

WHAT'S RUNNING RIGHT NOW
  from era_dispatch import current_job
  current_job()   # -> {"brand":..., "task_type":..., "order":..., "started":...} or None
"""

import threading
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import YAML

import era_dispatch as ed

BRANDS_FILE = Path(__file__).with_name("brands.yaml")

_yaml = YAML()
_yaml.preserve_quotes = True  # edits stay minimal; comments in brands.yaml survive

_scheduler = BackgroundScheduler()
_edit_lock = threading.Lock()

# maps a schedules{} key in brands.yaml -> (task_type for run_task, spoken order text)
TASK_MAP = {
    "weekly_plan": ("weekly", "Prepare this week's content plan and production."),
    "auto_publish": ("publish", "Publish all approved files now."),
}


def _load():
    with open(BRANDS_FILE, encoding="utf-8") as f:
        return _yaml.load(f)


def _run_job(brand_key, task_key):
    task_type, order = TASK_MAP[task_key]
    result = ed.run_task(order, task_type=task_type, brand_key=brand_key)
    # TODO: hand `result` to Era's notification layer (voice/Gmail summary already
    # happens inside the brand's own pipeline per WORKFLOW.md; this is just for
    # Era's own log/voice confirmation that the scheduled run happened).
    status = "ok" if result.get("ok") else f"FAILED: {result.get('error')}"
    print(f"[era_scheduler] {time.strftime('%Y-%m-%d %H:%M')} {brand_key}/{task_key} -> {status}")
    return result


# ── global (non-brand) jobs ─────────────────────────────────────────────────
# Everything above is per-brand. Vault maintenance is not: the vault is one
# shared artifact, and auditing it once per brand would repeat the same work and
# write the same health note twice. It lives under `defaults.schedules` instead.


def _run_vault_maintenance():
    """Audit the Obsidian vault and repair what has exactly one right answer.

    `apply=True` on purpose, and it is safe because of where the auto-fix line is
    drawn: the only two actions are adding a `brand:` field the note's own folder
    already dictates, and moving a note the USER marked `status: done` into
    Archive. Nothing is ever deleted and no body text is ever rewritten — see
    core/vault_maintenance.py's deletion rule. Everything ambiguous is reported
    into Areas/_Global/Era_Vault_Health.md and left alone.
    """
    from core import vault_maintenance

    report = vault_maintenance.sync(apply=True)
    print(
        f"[era_scheduler] {time.strftime('%Y-%m-%d %H:%M')} vault_maintenance -> {report.summary()}"
    )

    # Only an ERROR earns an interruption. Warnings ("85% of the brief budget")
    # are for the health note; a nightly push about them trains you to ignore
    # the channel that also carries "this note is in no brief at all".
    if report.errors:
        try:
            from app.notify import _tg_notify

            lines = "\n".join(f"• {f.detail}" for f in report.errors[:5])
            _tg_notify(f"Vault needs attention — {report.summary()}\n{lines}")
        except Exception as exc:
            print(f"[era_scheduler] vault alert not sent: {exc}")
    return report


GLOBAL_TASK_MAP = {"vault_maintenance": _run_vault_maintenance}


def _register_global(cfg, registered):
    """Register the jobs under `defaults.schedules`. Absent block = nothing on."""
    for task_key, sched in ((cfg.get("defaults") or {}).get("schedules") or {}).items():
        if not sched or not sched.get("enabled") or task_key not in GLOBAL_TASK_MAP:
            continue
        _scheduler.add_job(
            GLOBAL_TASK_MAP[task_key],
            CronTrigger.from_crontab(sched["cron"]),
            id=f"_global:{task_key}",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        registered.append(("_global", task_key, sched["cron"]))


def reload():
    """Re-read brands.yaml and re-register every ENABLED schedule. Safe to call
    any time (e.g. after set_schedule, or after you hand-edit brands.yaml)."""
    cfg = _load()
    _scheduler.remove_all_jobs()
    registered = []
    _register_global(cfg, registered)
    for brand_key, brand in (cfg.get("brands") or {}).items():
        for task_key, sched in (brand.get("schedules") or {}).items():
            if not sched or not sched.get("enabled") or task_key not in TASK_MAP:
                continue
            trig = CronTrigger.from_crontab(sched["cron"])
            _scheduler.add_job(
                _run_job,
                trig,
                args=[brand_key, task_key],
                id=f"{brand_key}:{task_key}",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            registered.append((brand_key, task_key, sched["cron"]))
    return registered


def start_scheduler():
    if not _scheduler.running:
        _scheduler.start()
    return reload()


def set_schedule(brand_key, task_key, cron=None, enabled=None):
    """Edit ONE brand's ONE schedule (cron and/or enabled) in brands.yaml, keeping
    the file's formatting/comments intact, then hot-reload. This is what Era calls
    when you say e.g. 'move Delta Cuisine's weekly plan to Thursday 10am.'
    Returns the updated schedule dict, or raises KeyError if brand/task unknown."""
    if task_key not in TASK_MAP:
        raise KeyError(f'Unknown task_key "{task_key}". Use one of {list(TASK_MAP)}.')
    with _edit_lock:
        cfg = _load()
        if brand_key not in cfg.get("brands", {}):
            raise KeyError(f'Unknown brand "{brand_key}".')
        sched = cfg["brands"][brand_key].setdefault("schedules", {}).setdefault(task_key, {})
        if cron is not None:
            sched["cron"] = cron
        if enabled is not None:
            sched["enabled"] = enabled
        with open(BRANDS_FILE, "w", encoding="utf-8") as f:
            _yaml.dump(cfg, f)
    reload()
    return dict(sched)


def list_schedules():
    """Everything currently registered — for 'what's scheduled?' voice queries."""
    cfg = _load()
    out = []
    for task_key, sched in ((cfg.get("defaults") or {}).get("schedules") or {}).items():
        if task_key in GLOBAL_TASK_MAP:
            out.append(
                {
                    "brand": "_global",
                    "task": task_key,
                    "cron": sched.get("cron"),
                    "enabled": bool(sched.get("enabled")),
                }
            )
    for brand_key, brand in (cfg.get("brands") or {}).items():
        for task_key, sched in (brand.get("schedules") or {}).items():
            if task_key in TASK_MAP:
                out.append(
                    {
                        "brand": brand_key,
                        "task": task_key,
                        "cron": sched.get("cron"),
                        "enabled": bool(sched.get("enabled")),
                    }
                )
    return out


if __name__ == "__main__":
    jobs = start_scheduler()
    print("Registered:", jobs)
    print("Ctrl+C to stop this test run.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass

# DAILY_INSPIRATION (2026-09-14)
# Auto-fetch feature inspiration each morning; calls actions.feature_inspire.inspire()
