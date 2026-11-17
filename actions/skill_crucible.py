"""Skill synthesis through Era's existing staged, verified development pipeline."""

from __future__ import annotations

import re


def run(action: str, arguments: dict) -> dict:
    from core import development, feature_engine

    if action == "catalog":
        feature_engine.restore()
        return {
            "success": True,
            "skills": feature_engine.catalog(),
            "jobs": development.list_jobs(),
            "isolation": "Candidate workspace and subprocess tests; not an OS security sandbox.",
            "activation": "Verified candidates require the user's final approval in Development.",
        }
    if action == "forge":
        name, goal = arguments.get("name"), arguments.get("goal")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,47}", name):
            raise ValueError("Use a snake_case skill name (3–48 characters)")
        if not isinstance(goal, str) or not 10 <= len(goal.strip()) <= 4000:
            raise ValueError("Describe the skill in 10–4000 characters")
        request = (
            f"Create or improve an Era feature named {name}. User goal: {goal}\n"
            f"Use features/{name}.py with FEATURE={{'id': '{name}', 'description': '...'}} "
            "and run(arguments). Use existing Era services and credential policy. "
            f"Write tools/test_feature_{name}.py with meaningful success, failure and boundary assertions. "
            "Mock remote services; do not install packages, disable TLS validation, invoke external actions "
            "during import/tests, or turn errors into fabricated successful output. Declare missing "
            "dependencies explicitly. Keep source modifications limited to these feature/test files."
        )
        job = development.propose(
            request, [f"features/{name}.py", f"tools/test_feature_{name}.py"], reload=True
        )
        return {
            "success": True,
            "job": job,
            "message": "Skill generation started in staging; completion and activation are not yet proven. Review Development for tests and final approval.",
        }
    raise ValueError("Use forge or catalog; run activated skills with run_feature")
