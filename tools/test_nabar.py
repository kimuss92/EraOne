"""crew/nabar.py — the skeptic's report contract.

Nabar exists so the other bots do not simply agree. These tests pin the rules that make
that real rather than rhetorical:

  * a report is bound to the SHA-256 of the exact artifact — one changed byte and it is
    stale, whatever the artifact is called;
  * zero findings is NOT a pass: a review is complete only with a check that could have
    failed and was run, or an explicit reason there was nothing to test;
  * a check Nabar merely asserts (no recorded evidence) is discarded, and listed;
  * an unproven load-bearing assumption pauses the change; `justified` needs a real reason;
  * the deterministic checks the code runs itself catch real defects.
"""

from __future__ import annotations

import pytest

from crew import nabar
from crew.nabar import (
    Assumption,
    Check,
    NabarReport,
    artifact_sha256,
    build_report,
    builtin_checks,
    parse_json_object,
)

ARTIFACT = "replace the Cadence section with: seven reels a week"


def passing_check() -> Check:
    return Check("ran it", "builtin:x", "observed something real", True)


# ── hash binding ────────────────────────────────────────────────────────────
class TestHashBinding:
    def test_report_records_the_artifacts_sha256(self):
        rep = NabarReport("note_body", artifact_sha256(ARTIFACT), checks=[passing_check()])
        assert rep.artifact_sha256 == artifact_sha256(ARTIFACT)
        assert len(rep.artifact_sha256) == 64

    def test_valid_only_for_the_exact_artifact(self):
        rep = build_report("note_body", ARTIFACT, None, [passing_check()])
        assert rep.status_for(ARTIFACT) == "valid"

    @pytest.mark.parametrize(
        "changed",
        [ARTIFACT + " ", ARTIFACT.replace("seven", "eight"), ARTIFACT.upper(), ARTIFACT[:-1]],
    )
    def test_one_changed_byte_makes_it_stale(self, changed):
        rep = build_report("note_body", ARTIFACT, None, [passing_check()])
        assert rep.status_for(changed) == "stale"

    def test_stale_outranks_every_other_status(self):
        rep = build_report("x", ARTIFACT, {"assumptions": [{"text": "a", "load_bearing": True}]})
        assert rep.status_for("something else entirely") == "stale"

    def test_binding_is_to_content_not_to_a_name(self):
        a, b = "same bytes", "same bytes"
        rep = build_report("name_one", a, None, [passing_check()])
        assert rep.status_for(b) == "valid"  # a different "name" for identical bytes still binds

    def test_bytes_and_str_hash_alike(self):
        assert artifact_sha256("héllo") == artifact_sha256("héllo".encode())


# ── completeness: zero findings is not a pass ───────────────────────────────
class TestCompleteness:
    def test_no_findings_and_no_check_is_incomplete_not_valid(self):
        rep = build_report("crew_output", ARTIFACT, {"findings": []})
        assert rep.status_for(ARTIFACT) == "incomplete"

    def test_findings_alone_do_not_complete_a_review(self):
        raw = {"findings": [{"severity": "warn", "claim": "tone is off", "evidence": "line 2"}]}
        assert build_report("crew_output", ARTIFACT, raw).status_for(ARTIFACT) == "incomplete"

    def test_one_run_check_completes_it(self):
        assert build_report("x", ARTIFACT, None, [passing_check()]).status_for(ARTIFACT) == "valid"

    def test_a_real_reason_for_no_testable_surface_completes_it(self):
        raw = {"no_testable_surface": "a one-line tone note; nothing to execute or compare"}
        assert build_report("x", ARTIFACT, raw).status_for(ARTIFACT) == "valid"

    @pytest.mark.parametrize("reason", ["", "n/a", "nothing", "   ", "no surface"])
    def test_a_token_reason_does_not_count(self, reason):
        rep = build_report("x", ARTIFACT, {"no_testable_surface": reason})
        assert rep.status_for(ARTIFACT) == "incomplete"

    def test_a_check_that_could_not_have_failed_does_not_count(self):
        rigged = Check("always true", "builtin:y", "1 == 1", True, could_have_failed=False)
        assert build_report("x", ARTIFACT, None, [rigged]).status_for(ARTIFACT) == "incomplete"

    def test_a_check_with_nothing_observed_does_not_count(self):
        empty = Check("claimed", "builtin:y", "   ", True)
        assert build_report("x", ARTIFACT, None, [empty]).status_for(ARTIFACT) == "incomplete"


# ── a claimed check must have happened ──────────────────────────────────────
class TestEvidence:
    RAW = {
        "checks": [
            {
                "description": "ran the tests",
                "evidence_ref": "obs1",
                "observed": "12 passed",
                "passed": True,
            },
            {
                "description": "invented",
                "evidence_ref": "obs9",
                "observed": "trust me",
                "passed": True,
            },
            {"description": "no ref at all", "observed": "x", "passed": True},
        ]
    }

    def test_only_checks_citing_recorded_observations_count(self):
        rep = build_report("x", ARTIFACT, self.RAW, recorded_refs={"obs1"})
        assert [c.description for c in rep.checks] == ["ran the tests"]

    def test_fabricated_checks_are_discarded_and_listed(self):
        rep = build_report("x", ARTIFACT, self.RAW, recorded_refs={"obs1"})
        assert rep.discarded_checks == ["invented", "no ref at all"]

    def test_no_recorded_observations_means_no_check_counts(self):
        rep = build_report("x", ARTIFACT, self.RAW, recorded_refs=frozenset())
        assert rep.checks == [] and rep.status_for(ARTIFACT) == "incomplete"

    def test_a_builtin_check_needs_no_recorded_observation(self):
        rep = build_report("x", ARTIFACT, None, [passing_check()], recorded_refs=frozenset())
        assert rep.counted_checks()


# ── assumptions pause the change ────────────────────────────────────────────
class TestAssumptions:
    def report(self, *assumptions):
        raw = {"assumptions": list(assumptions)}
        return build_report("x", ARTIFACT, raw, [passing_check()])

    def test_unproven_load_bearing_assumption_pauses(self):
        rep = self.report(
            {"text": "the note is not synced mid-write", "load_bearing": True, "status": "unproven"}
        )
        assert rep.status_for(ARTIFACT) == "paused"
        assert rep.unresolved()[0].text.startswith("the note is not synced")

    def test_unproven_but_not_load_bearing_does_not_pause(self):
        rep = self.report({"text": "wording is fine", "load_bearing": False, "status": "unproven"})
        assert rep.status_for(ARTIFACT) == "valid"

    def test_proven_load_bearing_does_not_pause(self):
        rep = self.report({"text": "base hash matches", "load_bearing": True, "status": "proven"})
        assert rep.status_for(ARTIFACT) == "valid"

    def test_justified_needs_a_written_reason(self):
        thin = self.report(
            {"text": "x", "load_bearing": True, "status": "justified", "justification": "ok"}
        )
        solid = self.report(
            {
                "text": "x",
                "load_bearing": True,
                "status": "justified",
                "justification": "The note is Inbox-only and reviewed by hand.",
            }
        )
        assert thin.status_for(ARTIFACT) == "paused"
        assert solid.status_for(ARTIFACT) == "valid"

    def test_unknown_status_is_treated_as_unproven(self):
        rep = self.report({"text": "x", "load_bearing": True, "status": "probably fine"})
        assert rep.status_for(ARTIFACT) == "paused"

    def test_paused_outranks_incomplete(self):
        raw = {"assumptions": [{"text": "x", "load_bearing": True, "status": "unproven"}]}
        assert build_report("x", ARTIFACT, raw).status_for(ARTIFACT) == "paused"

    def test_assumption_settled_property(self):
        assert Assumption("a", True, "proven").settled
        assert not Assumption("a", True, "unproven").settled


# ── the deterministic checks the code runs itself ───────────────────────────
ORIGINAL = "---\nbrand: delta_cuisine\ntype: reference\nstatus: active\n---\n# T\n\n" + "body " * 80


class TestBuiltinChecks:
    def run(self, original, proposed, current=None, base=None):
        current = original if current is None else current
        base = artifact_sha256(original) if base is None else base
        return {
            c.evidence_ref: c
            for c in builtin_checks(
                "vault_replace",
                "diff",
                {
                    "original": original,
                    "proposed": proposed,
                    "current": current,
                    "base_sha256": base,
                },
            )
        }

    def test_clean_replacement_passes_every_check(self):
        proposed = ORIGINAL.replace("body body", "better body")
        assert all(c.passed for c in self.run(ORIGINAL, proposed).values())

    def test_lost_frontmatter_key_fails(self):
        proposed = "---\ntype: reference\n---\n# T\n\n" + "body " * 80
        c = self.run(ORIGINAL, proposed)["builtin:frontmatter_kept"]
        assert not c.passed and "brand" in c.observed and "status" in c.observed

    def test_wiped_body_fails(self):
        c = self.run(ORIGINAL, "---\nbrand: delta_cuisine\ntype: reference\nstatus: active\n---\nx")
        assert not c["builtin:body_not_wiped"].passed

    def test_short_note_is_not_flagged_as_wiped(self):
        short = "---\ntype: reference\n---\nhi"
        assert self.run(short, "---\ntype: reference\n---\nok")["builtin:body_not_wiped"].passed

    def test_note_changed_since_staging_fails(self):
        c = self.run(ORIGINAL, ORIGINAL + "x", current=ORIGINAL + " a human edit")
        assert not c["builtin:base_unchanged"].passed

    def test_every_builtin_check_could_have_failed_and_says_what_it_saw(self):
        for c in self.run(ORIGINAL, ORIGINAL + "x").values():
            assert c.could_have_failed and c.observed.strip()

    def test_code_proposal_compile_check_catches_a_syntax_error(self):
        text = "Fix:\n```python\ndef ok():\n    return 1\n```\nand\n```python\ndef bad(:\n```\n"
        checks = builtin_checks("code_proposal", text)
        assert [c.passed for c in checks] == [True, False]
        assert "SyntaxError" in checks[1].observed

    def test_proposal_without_code_has_no_builtin_check(self):
        assert builtin_checks("code_proposal", "just prose") == []

    def test_unknown_kind_has_no_builtin_check(self):
        assert builtin_checks("crew_output", "x", {}) == []


# ── parsing what a model says ───────────────────────────────────────────────
class TestParsing:
    @pytest.mark.parametrize(
        "text",
        ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! {"a": 1} hope that helps'],
    )
    def test_extracts_one_object(self, text):
        assert parse_json_object(text) == {"a": 1}

    @pytest.mark.parametrize("text", ["", "no json here", "{broken", "[1, 2]", None, 5])
    def test_garbage_yields_none(self, text):
        assert parse_json_object(text) is None

    def test_build_report_survives_hostile_raw_output(self):
        raw = {
            "findings": "nope",
            "checks": [None, 3, {"evidence_ref": ["x"]}],
            "assumptions": [{"text": ""}, "x", {"text": "t", "load_bearing": "yes"}],
        }
        rep = build_report("x", ARTIFACT, raw, recorded_refs={"obs1"})
        assert isinstance(rep, NabarReport) and rep.findings == []

    def test_spoken_summary_names_failures_and_incompleteness(self):
        bad = Check("frontmatter kept", "builtin:f", "keys lost: ['brand']", False)
        raw = {"findings": [{"severity": "warn", "claim": "brand key dropped"}]}
        said = build_report("x", ARTIFACT, raw, [bad]).spoken()
        assert "1 failed" in said and "brand key dropped" in said
        assert "incomplete" in build_report("x", ARTIFACT, None).spoken()

    def test_report_serialises_for_the_tool_result(self):
        d = build_report("x", ARTIFACT, None, [passing_check()]).to_dict()
        assert d["artifact_sha256"] == artifact_sha256(ARTIFACT) and d["checks"][0]["passed"]
        assert nabar.NabarReport.__name__ == "NabarReport"
