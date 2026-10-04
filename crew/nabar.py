"""crew/nabar.py — what a Nabar review IS, as data the code can enforce.

Nabar is the crew's skeptic. His job is to stop the other bots agreeing with
everything, not to disagree on a schedule: useful and factual, not contrarian.

THE RULES (each is enforced in code, none lives only in a prompt)

  * BOUND TO A HASH, NOT A NAME. Every report records the SHA-256 of the exact
    artifact reviewed. `status_for(artifact)` recomputes it: change one byte after the
    review and the report is "stale" — re-review required.
  * ZERO FINDINGS IS NOT A PASS. A review is COMPLETE only if it records at least one
    falsifiable check (something that could have failed AND was actually run, with an
    observed result) or states why the change had no testable surface. Findings alone
    do not complete a review.
  * A CLAIMED CHECK MUST HAVE HAPPENED. A check counts only if its `evidence_ref` names
    a capability observation the runtime recorded in this very turn, or a `builtin:`
    check this module ran itself. A check Nabar merely asserts is discarded and listed.
  * ADVISORY, EXCEPT UNPROVEN LOAD-BEARING CLAIMS. Nothing waits on Nabar by default.
    If he marks a load-bearing assumption `unproven`, the change is PAUSED until it is
    fixed or justified in writing (`justified` needs a real reason, not a word).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

_MIN_REASON = 20  # characters of written justification / no-surface reason
_STATUSES = ("proven", "unproven", "justified")
_SEVERITIES = ("info", "warn", "block")
_FM_BLOCK = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_PY_FENCE = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _substantive(text: str) -> bool:
    """A written reason, not filler: long enough AND several different words
    ("n/a n/a n/a n/a n/a" is thirty characters of nothing)."""
    words = {w.lower() for w in re.findall(r"\w+", text or "")}
    return len((text or "").strip()) >= _MIN_REASON and len(words) >= 4


def _norm(text: str) -> str:
    return " ".join(str(text).split()).lower()


def artifact_sha256(artifact: str | bytes) -> str:
    data = artifact.encode("utf-8") if isinstance(artifact, str) else bytes(artifact)
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Finding:
    severity: str
    claim: str
    evidence: str = ""


@dataclass(frozen=True)
class Check:
    description: str
    evidence_ref: str  # "builtin:<name>" or an observation id the runtime recorded
    observed: str
    passed: bool
    could_have_failed: bool = True


@dataclass(frozen=True)
class Assumption:
    text: str
    load_bearing: bool
    status: str = "unproven"
    justification: str = ""

    @property
    def settled(self) -> bool:
        if self.status == "proven":
            return True
        return self.status == "justified" and _substantive(self.justification)


@dataclass
class NabarReport:
    artifact_kind: str
    artifact_sha256: str
    findings: list[Finding] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    no_testable_surface: str = ""
    llm_available: bool = True
    discarded_checks: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def counted_checks(self) -> list[Check]:
        return [c for c in self.checks if c.could_have_failed and c.observed.strip()]

    def unresolved(self) -> list[Assumption]:
        return [a for a in self.assumptions if a.load_bearing and not a.settled]

    def is_complete(self) -> bool:
        return bool(self.counted_checks()) or _substantive(self.no_testable_surface)

    def status_for(self, artifact: str | bytes) -> str:
        """stale | paused | incomplete | valid — checked in that order."""
        if artifact_sha256(artifact) != self.artifact_sha256:
            return "stale"
        if self.unresolved():
            return "paused"
        if not self.is_complete():
            return "incomplete"
        return "valid"

    def failed_checks(self) -> list[Check]:
        return [c for c in self.counted_checks() if not c.passed]

    def to_dict(self) -> dict:
        return asdict(self)

    def spoken(self) -> str:
        """One or two sentences a person can hear before approving."""
        counted = self.counted_checks()
        bad = self.failed_checks()
        who = "Nabar" if self.llm_available else "Nabar was unavailable; automatic checks"
        if not counted:
            bits = ["Nabar has not run a check."]  # never "all passed" about nothing
        else:
            bits = [f"{who} ran {len(counted)} check{'s' if len(counted) != 1 else ''}"]
            bits[0] += f", {len(bad)} failed." if bad else ", all passed."
        if bad:
            bits.append("WARNING, failed: " + "; ".join(c.description for c in bad[:2]) + ".")
        loud = [f.claim for f in self.findings if f.severity in ("warn", "block")][:2]
        if loud:
            bits.append("His concerns: " + "; ".join(loud) + ".")
        if self.unresolved():
            bits.append("He holds an unproven assumption: " + self.unresolved()[0].text + ".")
        if not self.is_complete():
            bits.append("His review is incomplete.")
        return " ".join(bits)


# ── parsing what the model said ─────────────────────────────────────────────
def parse_json_object(text: str) -> dict | None:
    """Pull one JSON object out of model text (fenced or bare). None if there isn't one."""
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _txt(value, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def build_report(
    kind: str,
    artifact: str | bytes,
    raw: dict | None,
    builtin: list[Check] | None = None,
    recorded_refs: Mapping[str, str] | set[str] | frozenset[str] = frozenset(),
    llm_available: bool = True,
) -> NabarReport:
    """Turn Nabar's raw JSON (or None) plus our own checks into an enforceable report."""
    report = NabarReport(
        artifact_kind=kind,
        artifact_sha256=artifact_sha256(artifact),
        checks=list(builtin or []),
        llm_available=llm_available,
    )
    raw = raw if isinstance(raw, dict) else {}
    for item in raw.get("findings") or []:
        if isinstance(item, dict) and _txt(item.get("claim")):
            sev = _txt(item.get("severity")).lower()
            report.findings.append(
                Finding(
                    sev if sev in _SEVERITIES else "info",
                    _txt(item.get("claim")),
                    _txt(item.get("evidence")),
                )
            )
    for item in raw.get("checks") or raw.get("falsifiable_checks") or []:
        if not isinstance(item, dict):
            continue
        ref = _txt(item.get("evidence_ref"), 80)
        desc = _txt(item.get("description"))
        observed = _txt(item.get("observed"))
        if ref not in recorded_refs:
            report.discarded_checks.append(desc or "(unnamed check)")
            continue  # asserted, never run
        if isinstance(recorded_refs, Mapping) and _norm(observed) not in _norm(recorded_refs[ref]):
            # He cited something that ran, but what he says it SHOWED is not in it. A
            # check is only as real as its observation: copy an excerpt of it, or it
            # does not count.
            report.discarded_checks.append(
                f"{desc or '(unnamed check)'} (observed text not in {ref})"
            )
            continue
        report.checks.append(
            Check(
                desc,
                ref,
                observed,
                bool(item.get("passed")),
                bool(item.get("could_have_failed", True)),
            )
        )
    for item in raw.get("assumptions") or []:
        if isinstance(item, dict) and _txt(item.get("text")):
            status = _txt(item.get("status")).lower()
            report.assumptions.append(
                Assumption(
                    _txt(item.get("text")),
                    bool(item.get("load_bearing", False)),
                    status if status in _STATUSES else "unproven",
                    _txt(item.get("justification"), 600),
                )
            )
    report.no_testable_surface = _txt(raw.get("no_testable_surface"), 600)
    return report


# ── checks the code runs itself ─────────────────────────────────────────────
def _fm_keys(text: str) -> set[str]:
    match = _FM_BLOCK.match(text)
    if not match:
        return set()
    return {
        ln.split(":", 1)[0].strip().lower()
        for ln in match.group(1).splitlines()
        if ":" in ln and not ln.startswith("#")
    }


def _body(text: str) -> str:
    match = _FM_BLOCK.match(text)
    return text[match.end() :] if match else text


def vault_replace_checks(original: str, proposed: str, current: str, base_sha: str) -> list[Check]:
    """Falsifiable checks on a staged replace. Each one could have failed."""
    checks = []
    now_sha = artifact_sha256(current)
    checks.append(
        Check(
            "the note is unchanged since the replacement was staged",
            "builtin:base_unchanged",
            f"staged hash {base_sha[:12]}, hash now {now_sha[:12]}",
            now_sha == base_sha,
        )
    )
    lost = sorted(_fm_keys(original) - _fm_keys(proposed))
    checks.append(
        Check(
            "frontmatter keys (brand/type/status decide what reaches a brief) survive",
            "builtin:frontmatter_kept",
            f"keys lost: {lost}" if lost else "no frontmatter key lost",
            not lost,
        )
    )
    old_len, new_len = len(_body(original).strip()), len(_body(proposed).strip())
    wiped = old_len >= 200 and new_len < old_len * 0.3
    checks.append(
        Check(
            "the replacement does not wipe most of the existing body",
            "builtin:body_not_wiped",
            f"body {old_len} -> {new_len} characters",
            not wiped,
        )
    )
    return checks


def code_proposal_checks(text: str) -> list[Check]:
    """Every fenced python block in a proposal must at least compile."""
    checks = []
    for n, block in enumerate(_PY_FENCE.findall(text or ""), start=1):
        try:
            compile(block, f"<proposal block {n}>", "exec")
            observed, ok = "compiled", True
        except SyntaxError as exc:
            observed, ok = f"SyntaxError: {exc.msg} (line {exc.lineno})", False
        checks.append(Check(f"python block {n} compiles", f"builtin:compile_{n}", observed, ok))
    return checks


def builtin_checks(kind: str, artifact: str, extra: dict | None = None) -> list[Check]:
    extra = extra or {}
    if kind == "vault_replace":
        return vault_replace_checks(
            extra.get("original", ""),
            extra.get("proposed", ""),
            extra.get("current", ""),
            extra.get("base_sha256", ""),
        )
    if kind == "code_proposal":
        return code_proposal_checks(artifact)
    return []
