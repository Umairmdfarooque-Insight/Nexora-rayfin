"""What happens next for a run in each state (FR-021)."""

from app.workflow.states import RunState

S = RunState

NEXT_ACTION: dict[RunState, str] = {
    S.CREATED: "Compile requirement",
    S.NEEDS_CLARIFICATION: "Answer open questions",
    S.REQUIREMENT_APPROVED: "Harvest metadata",
    S.DATA_PROFILED: "Build plan",
    S.PLAN_APPROVED: "Generate semantic model",
    S.MODEL_GENERATING: "Validate and review model",
    S.MODEL_FAILED: "Repair model",
    S.MODEL_APPROVED: "Generate report",
    S.REPORT_GENERATING: "Validate and review report",
    S.REPORT_FAILED: "Repair report",
    S.REPORT_APPROVED: "Deploy",
    S.DEPLOYING: "Wait for deployment",
    S.COMPLETED: "None",
    S.NEEDS_INPUT: "Provide input",
    S.ROLLED_BACK: "Investigate and redeploy",
}


def next_action(state: RunState) -> str:
    return NEXT_ACTION[state]


def _operation_label(operation: str) -> str:
    """A readable label for an `active_job.operation` string like
    `"POST /runs/{id}/compile"` -> `"compile"`."""
    _, _, path = operation.strip().partition(" ")
    segments = [s for s in path.split("/") if s and not s.startswith("{")]
    if segments and segments[0] == "runs":
        segments = segments[1:]
    return " ".join(segments).replace("-", " ") or "operation"


def next_action_for(run) -> str:
    """T-102 FR-5: while a job is queued or running, the next action is
    "Wait for <operation label>" instead of the state's usual action. T-702
    FR-3: a deployment job that ended `failed` while the run is still
    `DEPLOYING` was cancelled or crashed mid-deployment; the run stays there
    until a new `POST /runs/{id}/deploy` resumes the rollback."""
    job = run.active_job
    if job is not None and job.status in ("queued", "running"):
        return f"Wait for {_operation_label(job.operation)}"
    if run.state is S.DEPLOYING and job is not None and job.status == "failed":
        return "Resume rollback"
    return next_action(run.state)
