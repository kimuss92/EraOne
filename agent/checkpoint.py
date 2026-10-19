"""Process-local continuation owned by the executor, never model-supplied."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionCheckpoint:
    goal: str
    waiting_step: dict
    remaining_steps: list[dict]
    completed_steps: list[dict]
    step_results: dict
    skipped_errors: list[str]
    replan_attempts: int
