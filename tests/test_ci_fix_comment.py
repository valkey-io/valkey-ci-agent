

def test_render_handoff_includes_patch_and_language():
    from scripts.ci_fix.comment import render_comment
    from scripts.ci_fix.models import FixOutcome, FixPath, FixProposal, OutcomeKind

    proposal = FixProposal(path=FixPath.AUTHOR, failing_check="reply-schemas-validator",
                           root_cause="missing dep", reasoning="r", confidence=0.9,
                           build_command="m", verify_command="v")
    outcome = FixOutcome(
        kind=OutcomeKind.HANDOFF, summary="could not verify the fix here (no jsonschema)",
        proposal=proposal, handoff_patch="--- a/f\n+++ b/f\n+fix\n",
        failing_run_url="https://github.com/o/r/actions/runs/1",
    )
    body = render_comment(outcome)
    assert "handing it off" in body
    assert "could not verify" in body
    assert "+fix" in body            # the patch is included
    assert "I did not push this" in body
