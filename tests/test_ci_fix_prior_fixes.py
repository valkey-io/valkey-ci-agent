from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.ci_fix.models import FailedTest
from scripts.ci_fix.prior_fixes import (
    find_prior_fixes,
    render_prior_fixes,
    summarize_prior_fixes,
)
from tests.git_helpers import commit, git

TEST_FILE = "tests/unit/replication.tcl"
TEST = FailedTest("Replica reconnects after the primary restarts with a new run id", TEST_FILE)


def _clone_like_the_engine(tmp_path: Path) -> tuple[Path, Path, str]:
    """An upstream with ``unstable`` and a ``9.0`` release branch, and a clone of it.

    The clone has every branch as ``origin/*`` and ``origin/HEAD`` set, like
    the engine's blobless clone.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "unstable")
    git(upstream, "config", "user.name", "T")
    git(upstream, "config", "user.email", "t@example.com")
    commit(upstream, TEST_FILE, "test v1\n", "Add the replication test")
    git(upstream, "branch", "9.0")
    fork_point = git(upstream, "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    return upstream, clone, fork_point


def _refresh(clone: Path, ref: str) -> None:
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "--detach", ref)


def test_a_fix_on_the_default_branch_is_a_port_candidate_for_a_release_branch(tmp_path):
    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    commit(upstream, "src/server.c", "unrelated\n", "Speed up something")
    fix = commit(
        upstream, TEST_FILE, "test v2\n",
        f"Deflake replication test (#4684)\n\nThe test `{TEST.name}` raced the restart.\n\nFixes #4672",
    )
    _refresh(clone, "origin/9.0")

    fixes = find_prior_fixes(str(clone), (TEST,), issue_number=4672)

    assert [f.sha for f in fixes] == [fix]
    assert not fixes[0].on_target
    assert fixes[0].reasons == (
        f"changes `{TEST_FILE}`",
        f"names the test `{TEST.name}`",
        "references #4672",
    )
    assert "port candidate" in summarize_prior_fixes(fixes)
    rendered = render_prior_fixes(str(clone), fixes)
    assert "+test v2" in rendered and "NOT on the working commit" in rendered


def test_a_long_test_name_wrapped_in_the_commit_message_still_matches(tmp_path):
    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    fix = commit(
        upstream, "src/replication.c", "fixed\n",
        "Fix a race in replication\n\nThe test `Replica reconnects after the primary restarts\n"
        "with a new run id` failed because ...",
    )
    _refresh(clone, "origin/9.0")

    fixes = find_prior_fixes(str(clone), (TEST,))

    # The wrapped name matches by its leading words only: weaker evidence,
    # which the fix-word subject lifts over the bar.
    assert [f.sha for f in fixes] == [fix]
    assert fixes[0].reasons == ("names the start of the test, `Replica reconnects after the primary`",)


def test_a_fix_already_cherry_picked_to_the_release_branch_is_not_a_port(tmp_path):
    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    fix = commit(upstream, TEST_FILE, "test v2\n", "Deflake replication test (#4684)")
    git(upstream, "checkout", "-q", "9.0")
    git(upstream, "cherry-pick", "-x", fix)
    ported = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "-q", "unstable")
    _refresh(clone, "origin/9.0")

    fixes = find_prior_fixes(str(clone), (TEST,))

    assert [(f.sha, f.on_target, f.ported_as) for f in fixes] == [(fix, True, ported)]
    assert "already ported" in summarize_prior_fixes(fixes)


def test_a_fix_that_landed_after_the_failing_run_is_reported_as_already_there(tmp_path):
    upstream, clone, fork_point = _clone_like_the_engine(tmp_path)
    fix = commit(upstream, TEST_FILE, "test v2\n", "Fix flaky replication test")
    _refresh(clone, "origin/unstable")

    fixes = find_prior_fixes(str(clone), (TEST,), tested_sha=fork_point)

    assert [(f.sha, f.on_target) for f in fixes] == [(fix, True)]
    assert "already on the working commit" in summarize_prior_fixes(fixes)


def test_an_unrelated_change_to_the_file_alone_is_not_a_candidate(tmp_path):
    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    commit(upstream, TEST_FILE, "test v2\n", "Add another replication test")
    _refresh(clone, "origin/9.0")

    assert find_prior_fixes(str(clone), (TEST,)) == ()


def test_the_search_degrades_to_nothing_outside_a_repository(tmp_path):
    # None: no search ran, which the report must not call "nothing found".
    assert find_prior_fixes(str(tmp_path), (TEST,)) is None
    assert find_prior_fixes(str(tmp_path), ()) is None


def test_a_generic_subject_on_the_release_branch_is_not_taken_for_the_port(tmp_path):
    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    git(upstream, "checkout", "-q", "9.0")
    commit(upstream, "src/other.c", "x\n", "Fix flaky test")
    git(upstream, "checkout", "-q", "unstable")
    fix = commit(upstream, TEST_FILE, "test v2\n", "Fix flaky test")
    _refresh(clone, "origin/9.0")

    fixes = find_prior_fixes(str(clone), (TEST,))

    assert [(f.sha, f.on_target) for f in fixes] == [(fix, False)]


def test_a_backport_with_the_same_pr_subject_is_taken_for_the_port(tmp_path):
    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    fix = commit(upstream, TEST_FILE, "test v2\n", "Deflake replication test (#4684)")
    git(upstream, "checkout", "-q", "9.0")
    ported = commit(upstream, TEST_FILE, "test v2 adapted\n", "Deflake replication test (#4684)")
    git(upstream, "checkout", "-q", "unstable")
    _refresh(clone, "origin/9.0")

    fixes = find_prior_fixes(str(clone), (TEST,))

    assert [(f.sha, f.ported_as) for f in fixes] == [(fix, ported)]


def test_a_build_break_is_searched_by_the_files_its_log_names(tmp_path):
    from scripts.ci_fix.prior_fixes import paths_in_log

    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    commit(upstream, "src/replication.c", "v1\n", "Add replication")
    git(upstream, "branch", "-f", "9.0")
    fix = commit(upstream, "src/replication.c", "v2\n", "Fix the replication build on 32-bit (#99)")
    _refresh(clone, "origin/9.0")
    # As Valkey's CI prints them: gcc runs inside src/, ASan prints absolute
    # paths, valgrind prints frames; a file the checkout lacks is ignored.
    log = (
        "make[1]: Entering directory '/home/runner/work/valkey/valkey/src'\n"
        "replication.c:120:5: error: implicit declaration of function 'rdbSaveRio'\n"
        "    #0 0x55d in rdbSaveRio /home/runner/work/valkey/valkey/src/replication.c:131:9\n"
        "==123==    by 0x4F2: syncCommand (replication.c:140)\n"
        "missing.c:1:1: note: a file the checkout does not have\n"
    )

    paths = paths_in_log(str(clone), log)
    fixes = find_prior_fixes(str(clone), (), paths=paths)

    assert paths == ("src/replication.c",)
    assert [f.sha for f in fixes] == [fix]


def test_a_sibling_test_sharing_only_the_name_prefix_is_weak_evidence(tmp_path):
    upstream, clone, _ = _clone_like_the_engine(tmp_path)
    commit(
        upstream, "src/other.c", "x\n",
        "Speed up replication\n\nThe test `Replica reconnects after the primary restarts twice` was slow.",
    )
    _refresh(clone, "origin/9.0")

    # The prefix alone (2) under a subject with no fix words stays below the bar.
    assert find_prior_fixes(str(clone), (TEST,)) == ()


def test_a_log_path_leaving_the_checkout_is_ignored(tmp_path):
    from scripts.ci_fix.prior_fixes import paths_in_log

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.c").write_text("x")
    (tmp_path / "src" / "link.c").symlink_to("/etc/hosts")

    log = "src/../../../etc/hosts.c:1:1: error\nserver.c:3:1: error\nlink.c:1:1: error\n"

    assert paths_in_log(str(tmp_path), log) == ("src/server.c",)
