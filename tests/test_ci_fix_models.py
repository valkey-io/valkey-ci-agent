from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FailureType,
    FixCandidate,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    ResultKind,
    TargetKind,
)


def test_failure_run_url_binds_rerun_attempt() -> None:
    first = Failure("owner/repo", 12, "a" * 40, 34, "test")
    rerun = Failure("owner/repo", 12, "a" * 40, 34, "test", run_attempt=3)

    assert first.run_url == "https://github.com/owner/repo/actions/runs/12"
    assert rerun.run_url == "https://github.com/owner/repo/actions/runs/12/attempts/3"





def test_enum_values_are_stable_for_workflow_and_json_boundaries() -> None:
    assert FixPolicy.BACKPORT.value == "backport"
    assert TargetKind.UPDATE_PR.value == "update-pr"
    assert CandidateAction.REFUSE.value == "refuse"
    assert ResultKind.SUGGESTED.value == "suggested"
