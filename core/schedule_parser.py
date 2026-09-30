"""core/schedule_parser.py — turn "every monday at 9" into a trigger spec, in code.

The model only has to copy the user's phrase into `when`; the time maths happens
here, deterministically, so "tomorrow at 7" can never drift into the wrong day
because a model mis-added hours. Pure: no APScheduler import, so it is cheap to
test and core/task_scheduler.py builds the real trigger from the returned dict.

Spec shapes (all times are naive LOCAL time, which is what the user means):
  {"type": "date",     "run_at": "2026-09-18T07:00:00"}
  {"type": "cron",     "minute": 0, "hour": 9, "day": "*", "day_of_week": "mon"}
  {"type": "interval", "seconds": 7200}
Every spec also carries "description", a short human phrase for confirmations.

Raises ScheduleParseError (a ValueError) with a user-facing message on anything
it does not understand — a schedule is never guessed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


class ScheduleParseError(ValueError):
    pass


_DAYS = {
    "mon": ("monday", "lundi", "mon"),
    "tue": ("tuesday", "mardi", "tue", "tues"),
    "wed": ("wednesday", "mercredi", "wed"),
    "thu": ("thursday", "jeudi", "thu", "thur", "thurs"),
    "fri": ("friday", "vendredi", "fri"),
    "sat": ("saturday", "samedi", "sat"),
    "sun": ("sunday", "dimanche", "sun"),
}
_DAY_WORD = {w: key for key, words in _DAYS.items() for w in words}
_DAY_INDEX = {k: i for i, k in enumerate(_DAYS)}
_DAY_NAME = {k: words[0].title() for k, words in _DAYS.items()}

_UNITS = {
    "second": 1,
    "sec": 1,
    "seconde": 1,
    "minute": 60,
    "min": 60,
    "hour": 3600,
    "hr": 3600,
    "heure": 3600,
    "day": 86400,
    "jour": 86400,
    "week": 604800,
    "semaine": 604800,
}

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("january", "jan", "janvier"),
            ("february", "feb", "fevrier", "février"),
            ("march", "mar", "mars"),
            ("april", "apr", "avril"),
            ("may", "mai"),
            ("june", "jun", "juin"),
            ("july", "jul", "juillet"),
            ("august", "aug", "aout", "août"),
            ("september", "sep", "sept", "septembre"),
            ("october", "oct", "octobre"),
            ("november", "nov", "novembre"),
            ("december", "dec", "decembre", "décembre"),
        ]
    )
    for m in names
}

_TIME_RE = re.compile(
    r"\b(?P<h>\d{1,2})(?:(?::|h)(?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?(?![\d/-])",
    re.IGNORECASE,
)
_DEFAULT_TIME = (9, 0)


def _fmt_time(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _unit_seconds(word: str) -> int | None:
    w = word.lower().rstrip("s")
    return _UNITS.get(w)


def _extract_time(text: str) -> tuple[tuple[int, int] | None, str]:
    """Pull a clock time out of `text`. Returns ((hour, minute) | None, rest)."""
    if re.search(r"\b(noon|midi)\b", text):
        return (12, 0), re.sub(r"\b(at\s+|à\s+|a\s+)?(noon|midi)\b", " ", text)
    if re.search(r"\b(midnight|minuit)\b", text):
        return (0, 0), re.sub(r"\b(at\s+|à\s+|a\s+)?(midnight|minuit)\b", " ", text)
    for match in _TIME_RE.finditer(text):
        h = int(match.group("h"))
        m = int(match.group("m") or 0)
        ampm = (match.group("ampm") or "").lower().replace(".", "")
        has_clock_marker = bool(match.group("m") or ampm)
        # A bare number is only a time right after "at"/"à" ("at 9"); otherwise
        # "every 2 hours" or "the 15th" would be read as a clock time.
        before = text[: match.start()].rstrip()
        if not has_clock_marker and not re.search(r"(\bat|\bà|\ba|@)$", before):
            continue
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        if h > 23 or m > 59:
            raise ScheduleParseError(f"'{match.group(0).strip()}' is not a valid time.")
        prefix = re.sub(r"(\bat|\bà|@)\s*$", "", text[: match.start()])
        return (h, m), prefix + " " + text[match.end() :]
    return None, text


def _next_at(now: datetime, h: int, m: int) -> datetime:
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _date_spec(run_at: datetime, now: datetime) -> dict:
    if run_at <= now:
        raise ScheduleParseError("That time has already passed.")
    return {
        "type": "date",
        "run_at": run_at.replace(microsecond=0).isoformat(),
        "description": run_at.strftime("once on %A %d %B %Y at %H:%M"),
    }


def _days_in(text: str) -> list[str]:
    found = []
    for word in re.findall(r"[a-zé]+", text):
        key = _DAY_WORD.get(word.rstrip("s")) or _DAY_WORD.get(word)
        if key and key not in found:
            found.append(key)
    return sorted(found, key=_DAY_INDEX.get)


def parse_when(text: str, now: datetime | None = None) -> dict:
    """Parse a spoken schedule phrase into a trigger spec (see module docstring)."""
    now = now or datetime.now()
    raw = (text or "").strip()
    if not raw:
        raise ScheduleParseError("I need to know when to run it.")
    s = re.sub(r"\s+", " ", raw.lower()).strip(" .!?")

    # Explicit cron: "cron: 0 9 * * mon-fri" or a bare 5-field expression.
    # Minute, hour and day must be numeric/wildcard, so a five-word sentence like
    # "every monday at 10 am" can never be mistaken for a cron expression.
    cron = re.fullmatch(
        r"(?:cron:?\s*)?([\d*/,\-]+ [\d*/,\-]+ [\d*/,\-]+ [\d*/,\-a-z]+ [\d*/,\-a-z]+)", s
    )
    if cron:
        minute, hour, day, month, dow = cron.group(1).split()
        return {
            "type": "cron",
            "minute": minute,
            "hour": hour,
            "day": day,
            "month": month,
            "day_of_week": dow,
            "description": f"on cron '{cron.group(1)}'",
        }

    # Relative one-shot: "in 20 minutes", "dans 2 heures".
    rel = re.fullmatch(r"(?:in|dans) (\d+|an?|une?) ([a-zé]+)", s)
    if rel:
        n = 1 if rel.group(1) in ("a", "an", "un", "une") else int(rel.group(1))
        unit = _unit_seconds(rel.group(2))
        if not unit or n <= 0:
            raise ScheduleParseError(f"I don't understand the duration '{raw}'.")
        return _date_spec(now + timedelta(seconds=n * unit), now)

    recurring = re.match(
        r"(every|each|chaque|tous les|toutes les|daily|weekly|monthly|hourly|everyday)\b", s
    )
    if recurring:
        return _parse_recurring(s, raw, now)

    time_hm, rest = _extract_time(s)
    rest = re.sub(r"\s+", " ", rest).strip()

    # Absolute date: 2026-09-18 [14:30], or "september 18" / "18 september".
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})(?:t(\d{2}):(\d{2}))?", s)
    if iso:
        y, mo, d = (int(iso.group(i)) for i in (1, 2, 3))
        if iso.group(4):
            time_hm = (int(iso.group(4)), int(iso.group(5)))
        h, m = time_hm or _DEFAULT_TIME
        try:
            return _date_spec(datetime(y, mo, d, h, m), now)
        except ValueError as exc:
            raise ScheduleParseError(f"'{raw}' is not a valid date.") from exc

    named = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th|er)? ([a-zéû]+)\b|\b([a-zéû]+) (\d{1,2})(?:st|nd|rd|th)?\b",
        rest,
    )
    if named:
        day_s, month_s = (
            (named.group(1), named.group(2)) if named.group(1) else (named.group(4), named.group(3))
        )
        if month_s in _MONTHS:
            h, m = time_hm or _DEFAULT_TIME
            try:
                candidate = datetime(now.year, _MONTHS[month_s], int(day_s), h, m)
            except ValueError as exc:
                raise ScheduleParseError(f"'{raw}' is not a valid date.") from exc
            if candidate <= now:
                candidate = candidate.replace(year=now.year + 1)
            return _date_spec(candidate, now)

    if re.search(r"\b(tomorrow|demain)\b", rest):
        h, m = time_hm or _DEFAULT_TIME
        day = now + timedelta(days=1)
        return _date_spec(day.replace(hour=h, minute=m, second=0, microsecond=0), now)

    if re.search(r"\b(today|tonight|this (morning|afternoon|evening)|aujourd'hui|ce soir)\b", rest):
        if not time_hm:
            raise ScheduleParseError("What time today?")
        h, m = time_hm
        if "tonight" in rest or "ce soir" in rest or "evening" in rest or "afternoon" in rest:
            h = h + 12 if h < 12 else h
        return _date_spec(now.replace(hour=h, minute=m, second=0, microsecond=0), now)

    days = _days_in(rest)
    if len(days) == 1:
        h, m = time_hm or _DEFAULT_TIME
        ahead = (_DAY_INDEX[days[0]] - now.weekday()) % 7
        candidate = (now + timedelta(days=ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=7)
        return _date_spec(candidate, now)

    if time_hm and not re.search(r"[a-z]{3,}", re.sub(r"\b(at|on|à|le)\b", "", rest)):
        return _date_spec(_next_at(now, *time_hm), now)

    raise ScheduleParseError(
        f"I couldn't work out when '{raw}' is. Try 'at 14:30', 'tomorrow at 9', "
        "'in 20 minutes', 'every day at 8', 'every monday at 10' or 'every month on the 1st'."
    )


def _parse_recurring(s: str, raw: str, now: datetime) -> dict:
    # Fixed interval: "every 2 hours", "every 15 minutes", "hourly", "toutes les 10 minutes".
    interval = re.fullmatch(
        r"(?:every|each|toutes les|tous les|chaque) (\d+ )?(seconds?|secs?|minutes?|mins?|hours?|hrs?|heures?|minutes?)",
        s,
    )
    if interval or s == "hourly":
        n = int(interval.group(1)) if interval and interval.group(1) else 1
        unit = 3600 if s == "hourly" else _unit_seconds(interval.group(2))
        seconds = n * unit
        if seconds < 60:
            raise ScheduleParseError("The shortest repeat I allow is every minute.")
        label = interval.group(2).rstrip("s") if interval else "hour"
        return {
            "type": "interval",
            "seconds": seconds,
            "description": f"every {n} {label}{'s' if n > 1 else ''}"
            if n > 1
            else f"every {label}",
        }
    multi_day = re.fullmatch(
        r"(?:every|each|toutes les|tous les) (\d+) (days?|jours?|weeks?|semaines?)(.*)", s
    )
    if multi_day:
        n = int(multi_day.group(1))
        time_hm, _ = _extract_time(multi_day.group(3))
        seconds = n * _unit_seconds(multi_day.group(2))
        spec = {
            "type": "interval",
            "seconds": seconds,
            "description": f"every {n} {multi_day.group(2)}",
        }
        if time_hm:
            start = _next_at(now, *time_hm)
            spec["start_at"] = start.isoformat()
            spec["description"] += f" at {_fmt_time(*time_hm)}"
        return spec

    time_hm, rest = _extract_time(s)
    h, m = time_hm or _DEFAULT_TIME
    at = f" at {_fmt_time(h, m)}"

    if re.search(r"\b(month|monthly|mois)\b", rest):
        dom = re.search(r"\b(\d{1,2})(?:st|nd|rd|th|er)?\b", rest)
        if re.search(r"\blast day\b|\bdernier jour\b", rest):
            day = "last"
        else:
            day = int(dom.group(1)) if dom else now.day
            if not 1 <= int(day) <= 31:
                raise ScheduleParseError(f"'{raw}' has an invalid day of the month.")
        label = "the last day" if day == "last" else f"day {day}"
        return {
            "type": "cron",
            "minute": m,
            "hour": h,
            "day": str(day),
            "month": "*",
            "day_of_week": "*",
            "description": f"every month on {label}{at}",
        }

    if re.search(r"\b(weekdays?|workdays?|semaine de travail|jours? ouvrables?)\b", rest):
        dow, label = "mon-fri", "every weekday"
    elif re.search(r"\b(weekends?|week-ends?)\b", rest):
        dow, label = "sat,sun", "every weekend day"
    else:
        days = _days_in(rest)
        if days:
            dow = ",".join(days)
            label = "every " + ", ".join(_DAY_NAME[d] for d in days)
        elif re.search(r"\b(week|weekly|semaine)\b", rest):
            key = list(_DAYS)[now.weekday()]
            dow, label = key, f"every {_DAY_NAME[key]}"
        elif re.search(r"\b(day|daily|everyday|jours?|morning|evening|night|soir|matin)\b", rest):
            dow, label = "*", "every day"
        else:
            raise ScheduleParseError(
                f"I couldn't work out how often '{raw}' repeats. Try 'every day at 8', "
                "'every monday at 10', 'every 2 hours' or 'every month on the 1st'."
            )
    return {
        "type": "cron",
        "minute": m,
        "hour": h,
        "day": "*",
        "month": "*",
        "day_of_week": dow,
        "description": label + at,
    }
