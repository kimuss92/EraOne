"""Exercise the real staging/approval worker in disposable copies, without an LLM.

Run: python tools/verify_autonomous_development.py [--frontend]
Requires the installed test dependencies; --frontend also requires Node/npm.
This uses authored candidate code, not a fake verifier or a real Claude request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from core import development as dev
    from core import feature_engine
    from tools import era_manifest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", action="store_true")
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="era-development-verification-") as temporary:
        root = Path(temporary) / "repository"
        dev._copy_source(root)
        dev.ROOT = root
        dev.JOBS = root / "runtime/development"
        feature_engine.ROOT = root
        era_manifest.REPO = root
        era_manifest.JSON_PATH = root / "runtime/manifest.json"
        era_manifest.MD_PATH = root / "docs/MANIFEST.md"
        source = 'FEATURE = {"id": "verification", "description": "Double a value"}\n\ndef run(arguments):\n    return {"value": arguments["value"] * 2}\n'
        test = 'from features.verification_probe import run\n\n\ndef test_doubles():\n    assert run({"value": 7}) == {"value": 14}\n'
        plan = {
            "plan": "Verify a reloadable feature",
            "files": [
                {"path": "features/verification_probe.py", "content": source},
                {"path": "tools/test_verification_probe.py", "content": test},
            ],
            "tests": ["tools/test_verification_probe.py"],
        }
        if options.frontend:
            plan["files"].extend(
                [
                    {
                        "path": "actions/audio_control.py",
                        "content": (root / "actions/audio_control.py").read_text(encoding="utf-8")
                        + "\n# Candidate verification probe.\n",
                    },
                    {
                        "path": "ui_src/src/components/VisualWorkspace.tsx",
                        "content": (root / "ui_src/src/components/VisualWorkspace.tsx")
                        .read_text(encoding="utf-8")
                        .replace("LIVE WORKSPACE</span>", "LIVE WORKSPACE VERIFIED</span>"),
                    },
                ]
            )
        job = {
            "id": "e" * 16,
            "request": "Verification probe",
            "state": "planning",
            "progress": 0,
            "plan": "",
            "files": [],
            "checks": {},
            "reload": True,
            "approval": "",
        }
        dev._active = job["id"]
        dev.prepare(job, plan)
        dev._active = None
        assert job["state"] == "awaiting_approval", job["checks"]
        assert not (root / "features/verification_probe.py").exists()
        try:
            dev.activate(job["id"], job["approval"])
        except ValueError:
            pass
        else:
            raise AssertionError("Activation bypassed final approval")
        job = dev.activate(job["id"], job["approval"], confirmed=True)
        if options.frontend:
            assert job["requires_restart"] and job["checks"]["frontend"]
            assert not feature_engine.catalog(), "Mixed engine edits must wait for restart"
            feature_engine.restore()
            assert not feature_engine.catalog(), "Restore must not bypass restart"
            # A new engine process gets a new session ID; exercise this guard.
            feature_engine.SESSION_ID = "new-engine-session"
            feature_engine.restore()
        assert asyncio.run(feature_engine.run("verification", {"value": 9})) == {"value": 18}
        negative = {
            "id": "d" * 16,
            "request": "Reject failing tests",
            "state": "planning",
            "progress": 0,
            "plan": "",
            "files": [],
            "checks": {},
            "reload": True,
            "approval": "",
        }
        failed_plan = {
            "plan": "Intentionally failing regression",
            "files": [
                {
                    "path": "features/failed_probe.py",
                    "content": source.replace('"verification"', '"failedprobe"'),
                },
                {
                    "path": "tools/test_failed_probe.py",
                    "content": "def test_failure():\n    assert 1 == 2\n",
                },
            ],
            "tests": ["tools/test_failed_probe.py"],
        }
        dev._active = negative["id"]
        dev.prepare(negative, failed_plan)
        dev._active = None
        assert negative["state"] == "failed" and not negative["checks"]["tests"]
        assert not (root / "features/failed_probe.py").exists()
        report = {
            "passed": True,
            "real_worker": True,
            "frontend_requested": options.frontend,
            "positive_checks": job["checks"],
            "failing_candidate_rejected": True,
            "live_source_unchanged_until_approval": True,
            "explicit_approval_required": True,
            "feature_result": {"value": 18},
            "mixed_python_changes_require_restart": options.frontend,
        }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
