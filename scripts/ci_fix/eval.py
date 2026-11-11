"""Replay failures whose fix is known, and compare what the bot prepares with it.

The cases are in ``eval_cases.yml``: one exact failed job each, and the fix a
person merged or proposed for it. A replay runs the same engine a real fix
does, through ``ci-fix-run.yml``, and stops before publication:

1. plan: this module is the ``eval`` front door of ``scripts.ci_fix.run``
   (:func:`plan_request`). The failed job is resolved from its own workflow
   file like any other, so the fix is prepared inside that job where it can
   be. A case on the default branch is planned as of its failed commit
   (``FixTask.as_of_failure``), so the fix that landed later is not found.
2. the fix itself, unchanged.
3. ``score``: compare the candidate with the known fix.

Nothing is published. ``scripts.ci_fix.run`` refuses to publish a plan from
:attr:`FrontDoor.EVAL`, and ``ci-fix-run.yml`` skips its publish job for one.

A score is ``match`` (the known port, or a patch that is mostly the known
fix's lines), ``differs`` (another change: a person has to read it, since a
different fix may be right, and the same fix written another way shares few
lines; the score says how many of the known fix's replaced lines it replaces
too), ``refused`` or ``failed``.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from github import Auth, Github

from scripts.ci_fix.engine import changed_text
from scripts.ci_fix.models import (
    CandidateAction,
    FailedTest,
    Failure,
    FixCandidate,
    FixOutcome,
    FixPlan,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    FrontDoor,
    TargetKind,
)
from scripts.ci_fix.state import load, save
from scripts.common.git_clone import is_full_sha
from scripts.common.github_client import retry_github_call
from scripts.common.valkey_ci import Source

logger = logging.getLogger(__name__)

CASES_FILE = Path(__file__).resolve().parent / "eval_cases.yml"
SCORE_FILE = "score.json"
# scripts.ci_fix.run's OUTCOME_FILE; that module imports this one.
_OUTCOME_FILE = "outcome.json"
_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]*")
# A patch is the known fix when at least this share of the lines it adds are
# that fix's: the bar a patch that adapts an upstream commit has to clear.
_MATCH_SHARE = 50
_PATCH_CHARS = 20_000


@dataclass(frozen=True)
class Case:
    """One failed job, and the fix known for it."""

    id: str
    about: str
    repo: str
    run_id: int
    job_id: int
    job: str
    sha: str
    base_branch: str
    pr: int = 0
    issue: int = 0
    # A scheduled Daily run, whose tests run with --accurate.
    scheduled: bool = False
    # A fix for a backport branch: an upstream change, ported.
    backport: bool = False
    test_name: str = ""
    test_file: str = ""
    # The known fix: the upstream commit to port, or the pull request a person wrote.
    expect_port: str = ""
    expect_pull: int = 0
    # What the bot lacks for this case, for whoever reads its result.
    gap: str = ""


@dataclass(frozen=True)
class Score:
    """How one replay's candidate compares with the known fix."""

    case: str
    verdict: str  # match, differs, refused or failed
    detail: str
    expected: str
    got: str = ""
    # For a patch: the share of its added lines that are the known fix's, the share of the lines
    # the known fix replaces that it replaces too, and the files both change.
    shared_percent: int = 0
    replaced_percent: int = 0
    shared_files: tuple[str, ...] = ()


def load_cases(path: Path = CASES_FILE) -> dict[str, Case]:
    """The cases by id. Raises ``ValueError`` for a case that could not be replayed or scored."""
    listed = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("cases") or []
    cases: dict[str, Case] = {}
    for item in listed:
        test, expect = item.get("test") or {}, item.get("expect") or {}
        case = Case(
            id=str(item.get("id") or ""), about=str(item.get("about") or ""), repo=str(item.get("repo") or ""),
            run_id=int(item.get("run_id") or 0), job_id=int(item.get("job_id") or 0), job=str(item.get("job") or ""),
            sha=str(item.get("sha") or ""), base_branch=str(item.get("base_branch") or ""),
            pr=int(item.get("pr") or 0), issue=int(item.get("issue") or 0),
            scheduled=bool(item.get("scheduled")), backport=bool(item.get("backport")),
            test_name=str(test.get("name") or ""), test_file=str(test.get("file") or ""),
            expect_port=str(expect.get("port") or ""), expect_pull=int(expect.get("pull") or 0),
            gap=str(item.get("gap") or ""),
        )
        problem = _problem(case)
        if problem or case.id in cases:
            raise ValueError(f"eval case {case.id!r}: {problem or 'its id is used twice'}")
        cases[case.id] = case
    return cases


def _problem(case: Case) -> str:
    """Why ``case`` cannot be replayed or scored ("" when it can)."""
    if not _ID_RE.fullmatch(case.id):
        return "its id is not lower-case letters, digits, dots and dashes"
    if not (case.repo and case.run_id > 0 and case.job_id > 0 and case.job and case.base_branch):
        return "it needs repo, run_id, job_id, job and base_branch"
    if not is_full_sha(case.sha):
        return "its sha is not a full commit SHA"
    if bool(case.expect_port) == bool(case.expect_pull):
        return "it expects exactly one of a port and a pull request"
    if case.expect_port and not (is_full_sha(case.expect_port) and case.backport):
        return "a port is a full commit SHA, expected of a backport case"
    return ""


def plan_case(case: Case, job_log: str) -> FixPlan:
    """The plan a front door would have made for ``case``'s failure, never to be published."""
    failure = Failure(
        repo_full_name=case.repo,
        run_id=case.run_id,
        head_sha=case.sha,
        job_id=case.job_id,
        job_name=case.job,
        base_branch=case.base_branch,
        description=(
            f"`{case.test_name}` in `{case.test_file}` failed in job `{case.job}`." if case.test_name
            else f"the failure in job `{case.job}`"
        ),
        pr_number=case.pr,
        issue_number=case.issue,
        failed_tests=(FailedTest.from_report(case.test_name, case.test_file),) if case.test_file else (),
        source=Source.DAILY if case.scheduled else None,
        workflow=Source.DAILY.value,
    )
    if case.backport:
        target = FixTarget(
            TargetKind.UPDATE_PR, case.repo, base_repo_full_name=case.repo,
            branch=f"agent/backport/sweep/{case.base_branch}", expected_sha=case.sha,
        )
        task = FixTask(failure, target, FixPolicy.BACKPORT)
    else:
        target = FixTarget(
            TargetKind.DRAFT_PR, case.repo, base_repo_full_name=case.repo,
            branch=f"agent/ci-fix/eval-{case.id}", expected_sha=case.sha,
        )
        task = FixTask(failure, target, FixPolicy.GENERAL, as_of_failure=True)
    return FixPlan(front_door=FrontDoor.EVAL, task=task, job_log=job_log)


def plan_request(gh: Any, artifact_client: Any, request: dict[str, Any], *, bot_login: str = "") -> FixPlan:  # noqa: ARG001
    """The plan for ``request`` (``{"case": id}``), as the ``eval`` front door of ``scripts.ci_fix.run``."""
    case_id = str(request.get("case") or "")
    cases = load_cases()
    if case_id not in cases:
        raise ValueError(f"no eval case {case_id!r}; the cases are: {', '.join(cases)}")
    case = cases[case_id]
    job_log = str(artifact_client.download_job_log(case.repo, case.job_id) or "")
    if not job_log.strip():
        raise ValueError(f"the log of job {case.job_id} is gone: eval case {case.id!r} has aged out")
    return plan_case(case, job_log)


def report_result(gh: Any, plan: FixPlan, result: FixResult) -> dict[str, Any]:  # noqa: ARG001
    """A replay reports nowhere: its result is the fix job's outcome, which ``score`` reads."""
    return {"repo": plan.task.failure.repo_full_name, "action": result.kind.value, "summary": result.summary}


def score_case(case: Case, outcome: FixOutcome, known: tuple[tuple[str, ...], str] = ((), "")) -> Score:
    """Compare ``outcome`` with ``case``'s known fix; ``known`` is a known pull request's files and patch."""
    expected = f"port of `{case.expect_port[:12]}`" if case.expect_port else f"pull request #{case.expect_pull}"
    candidate = outcome.candidate
    if candidate is None:
        return Score(case.id, "failed", outcome.error or "the fix ended without a candidate", expected)
    if candidate.action is CandidateAction.REFUSE:
        return Score(case.id, "refused", candidate.summary, expected, got="refusal")
    if case.expect_port:
        return _port_score(case, candidate, expected)
    return _patch_score(case, candidate, expected, known)


def _port_score(case: Case, candidate: FixCandidate, expected: str) -> Score:
    if candidate.action is CandidateAction.PORT:
        got, commit = f"port of `{candidate.port_commit[:12]}`", candidate.port_commit
    else:
        commit = candidate.adapts
        got = f"patch adapting `{commit[:12]}`" if commit else "patch of its own"
    if commit.lower() == case.expect_port.lower():
        return Score(case.id, "match", "It took the known upstream commit.", expected, got=got)
    return Score(case.id, "differs", "It did not take the known upstream commit.", expected, got=got)


def _patch_score(case: Case, candidate: FixCandidate, expected: str, known: tuple[tuple[str, ...], str]) -> Score:
    files, patch = known
    if candidate.action is CandidateAction.PORT:
        return Score(case.id, "differs", "It ported a commit where a person wrote a fix.", expected,
                     got=f"port of `{candidate.port_commit[:12]}`")
    shared_files = tuple(sorted(set(files) & set(candidate.changed_paths)))
    ours = changed_text(candidate.patch, "+")
    percent = round(100 * len(ours & changed_text(patch, "+")) / len(ours)) if ours else 0
    # The same fix written another way adds other lines, but replaces the same ones.
    theirs_removed = changed_text(patch, "-")
    replaced = (
        round(100 * len(theirs_removed & changed_text(candidate.patch, "-")) / len(theirs_removed))
        if theirs_removed else 0
    )
    got = "patch to " + ", ".join(f"`{path}`" for path in candidate.changed_paths)
    if shared_files and percent >= _MATCH_SHARE:
        verdict, detail = "match", f"{percent}% of the lines it adds are the known fix's."
    elif shared_files:
        verdict = "differs"
        detail = (
            f"It changes the same file another way: {percent}% of the lines it adds are the known fix's, "
            f"and it replaces {replaced}% of the lines the known fix replaces."
        )
    else:
        verdict, detail = "differs", "It changes other files than the known fix does: " + ", ".join(files) + "."
    return Score(case.id, verdict, detail, expected, got=got, shared_percent=percent,
                 replaced_percent=replaced, shared_files=shared_files)


def pull_request_change(gh: Any, case: Case) -> tuple[tuple[str, ...], str]:
    """The files ``case``'s known pull request changes, and its patch."""
    files = retry_github_call(
        lambda: list(gh.get_repo(case.repo).get_pull(case.expect_pull).get_files()),
        retries=2,
        description=f"read the files of #{case.expect_pull}",
    )
    names = tuple(str(file.filename) for file in files)
    # The API gives each file's hunks without its header; changed_text reads either.
    return names, "\n".join(str(getattr(file, "patch", "") or "") for file in files)


def render_score(case: Case, score: Score, outcome: FixOutcome, known_patch: str = "") -> str:
    """The replay's result as Markdown, for the run's summary: the verdict, then both changes to read."""
    lines = [
        f"### CI fix eval `{case.id}`: **{score.verdict}**",
        "",
        case.about,
        "",
        f"- Expected: {score.expected}",
        f"- Got: {score.got or 'nothing'}",
        f"- {score.detail}",
    ]
    if case.gap:
        lines.append(f"- Known gap: {case.gap}")
    candidate = outcome.candidate
    if candidate is not None and candidate.root_cause:
        lines += ["", f"**The bot's root cause:** {candidate.root_cause}"]
    if candidate is not None and candidate.verification:
        lines += ["", "**What the bot checked**", "", *(f"- {line}" for line in candidate.verification.splitlines() if line)]
    for title, patch in (("The bot's patch", candidate.patch if candidate else ""), ("The known fix", known_patch)):
        if patch:
            lines += ["", f"<details><summary>{title}</summary>", "", "````diff", patch[:_PATCH_CHARS], "````", "", "</details>"]
    return "\n".join(lines) + "\n"


def _github() -> Github:
    token = os.environ.get("TARGET_TOKEN", "")
    if not token:
        raise SystemExit("TARGET_TOKEN is required: any token that can read the case's repository")
    return Github(auth=Auth.Token(token))


def _case(case_id: str) -> Case:
    cases = load_cases()
    if case_id not in cases:
        raise SystemExit(f"no eval case {case_id!r}; the cases are: {', '.join(cases)}")
    return cases[case_id]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print the cases")
    score_ = commands.add_parser("score", help="compare a replay's outcome with the known fix")
    score_.add_argument("--case", required=True)
    score_.add_argument("--outcome", required=True, type=Path, help="the directory the fix wrote its outcome to")
    score_.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "list":
        for case in load_cases().values():
            print(f"{case.id}\t{case.about}")
        return 0

    case = _case(args.case)
    gh = _github()
    try:
        outcome = load(args.outcome / _OUTCOME_FILE, FixOutcome)
    except FileNotFoundError:
        outcome = FixOutcome(error="the fix job ended without a result")
    known = pull_request_change(gh, case) if case.expect_pull else ((), "")
    score = score_case(case, outcome, known)
    save(args.out / SCORE_FILE, score)
    report = render_score(case, score, outcome, known[1])
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
