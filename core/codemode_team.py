"""Explicit specialist requests share CodeMode ownership and approval authority."""

import re

from core.cancel_registry import bind
from crew.coordination import CURRENT_WORK, TeamWork

ORDER = re.compile(r"^(?:(?:ask|tell)\s+)?(laura|cody|drew|maya|tom|nabar)[,: ]+(.+)$", re.I | re.S)


def consult(owner, work_id, token, authority):
    from crew.runtime import run_tool

    match = ORDER.fullmatch(owner.text.strip())
    if match is None:
        return None
    work = TeamWork(work_id, "codemode", owner.text, {"era"}, authority=authority)
    reset = CURRENT_WORK.set(work)
    try:
        with bind(token):
            result = run_tool({"action": "ask", "bot": match[1].lower(), "task": match[2]})
        return {
            "success": result.success,
            "message": result.message[:6000],
            "work": work.public(),
            "review": result.data.get("review_status"),
        }
    finally:
        CURRENT_WORK.reset(reset)
