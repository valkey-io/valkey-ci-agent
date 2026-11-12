"""Per-branch evidence: what a release branch carries of a PR's change."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.backport import eligibility
from scripts.backport.eligibility import BranchHistory, MergedSourcePR
from scripts.backport.eligibility_evidence import (
    branch_evidence,
    is_docs_only_change,
    is_non_product_change,
    subject_identifiers,
)
from scripts.release_notes.models import MergedPR


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], subject: str) -> str:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
        _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


_SERVER = """\
static int ioThreadsActive(void) {
    return server.io_threads_num > 1;
}

int shutdownServer(int flags) {
    int rc = 0;
    rc = flushAppendOnlyFile(1);
    return rc;
}
"""


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, object]:
    """9.0 forks before unstable adds the io-threads-always-active config.

    The fix then edits shutdownServer, which 9.0 has, and names the config.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "unstable")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "T")
    _commit(upstream, {"src/server.c": _SERVER, "tests/unit/shutdown.tcl": "test {old} {}\n"}, "base (#1)")
    _git(upstream, "branch", "9.0")
    _commit(upstream, {"src/config.c": 'createBoolConfig("io-threads-always-active", 0),\n'},
            "add io-threads-always-active (#2)")
    fix = _commit(upstream, {"src/server.c": _SERVER.replace("int rc = 0;", "int rc = -1;")},
                  "Fix shutdown with `io-threads-always-active` (#3)")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(upstream), str(clone)],
                   check=True, capture_output=True)
    return {"clone": str(clone), "fix": fix}


def _source(sha: str, title: str, body: str = "", issues: tuple[str, ...] = ()) -> MergedSourcePR:
    return MergedSourcePR(
        pr=MergedPR(number=3, title=title, body=body, author="a", url="u", merge_commit_sha=sha),
        merged_at="2000-01-01T00:00:00Z",
        source_commit_shas=(sha,),
        linked_issues=issues,
    )


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["tests/support/server.tcl", "tests/instances.tcl"], True),
        (["src/unit/test_zset.cpp", ".github/workflows/ci.yml", "README.md"], True),
        (["tests/unit/type/zset.tcl", "src/t_zset.c"], False),
        ([], False),
    ],
)
def test_non_product_change(paths, expected):
    assert is_non_product_change(paths) is expected


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["README.md", "docs/design.md"], True),
        (["README.md", "tests/unit/type/zset.tcl"], False),
        ([".github/workflows/ci.yml"], False),
        ([], False),
    ],
)
def test_docs_only_change(paths, expected):
    assert is_docs_only_change(paths) is expected


def test_subject_identifiers_keep_names_the_code_has(repo):
    clone, fix = repo["clone"], repo["fix"]
    ids = subject_identifiers(
        clone, f"{fix}^", fix,
        ["Fix shutdown with `io-threads-always-active`",
         'Hangs in "Some test that does not exist". Also see some-made-up-name.'],
    )
    assert "io-threads-always-active" in ids
    assert "shutdownServer" in ids  # the function the diff edits
    assert "Some test that does not exist" not in ids
    assert "some-made-up-name" not in ids


def test_branch_evidence_reports_files_patch_and_identifiers(repo):
    clone, fix = repo["clone"], repo["fix"]
    evidence = branch_evidence(
        clone, "refs/remotes/origin/9.0", "9.0", base=f"{fix}^", sha=fix,
        identifiers=["io-threads-always-active", "shutdownServer"],
    )
    assert evidence.files_present == ("src/server.c",)
    assert evidence.files_missing == ()
    assert evidence.applies_cleanly is True
    assert evidence.identifiers_present == ("shutdownServer",)
    assert evidence.identifiers_missing == ("io-threads-always-active",)
    assert evidence.carries_subject


def test_branch_evidence_patch_that_does_not_apply(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "unstable")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "T")
    _commit(upstream, {"a.c": "one\ntwo\n"}, "base")
    _git(upstream, "checkout", "-q", "-b", "9.0")
    _commit(upstream, {"a.c": "uno\ntwo\n"}, "diverge on 9.0")
    _git(upstream, "checkout", "-q", "unstable")
    fix = _commit(upstream, {"a.c": "ONE\ntwo\n"}, "fix one")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(upstream), str(clone)],
                   check=True, capture_output=True)
    evidence = branch_evidence(str(clone), "refs/remotes/origin/9.0", "9.0",
                               base=f"{fix}^", sha=fix, identifiers=[])
    assert evidence.applies_cleanly is False
    assert evidence.carries_subject  # nothing named, nothing to rule the branch out


def test_evidence_drops_a_branch_without_the_named_config(repo, monkeypatch):
    clone, fix = repo["clone"], repo["fix"]
    source = _source(fix, "Fix shutdown hang with `io-threads-always-active`",
                     issues=("#9 Hang\nOnly with io-threads-always-active yes.",))
    candidate = eligibility.assess(clone, source, ["9.0"], base=f"{fix}^", history=BranchHistory(clone))
    # shutdownServer is on 9.0, so 9.0 stays a candidate, with the evidence attached.
    assert isinstance(candidate, eligibility.Candidate)
    assert candidate.branches == {"9.0"}
    [evidence] = candidate.evidence
    assert "io-threads-always-active" in evidence.identifiers_missing

    # When the config is all the PR is about, 9.0 carries none of it.
    monkeypatch.setattr(eligibility, "subject_identifiers", lambda *_: ("io-threads-always-active",))
    dropped = eligibility.assess(clone, source, ["9.0"], base=f"{fix}^", history=BranchHistory(clone))
    assert dropped == {
        "action": "not-eligible",
        "detail": "none of io-threads-always-active exist on 9.0",
    }


def test_test_only_pr_is_a_marked_candidate(repo):
    clone = repo["clone"]
    upstream = Path(clone).parent / "upstream"
    fix = _commit(upstream, {"tests/unit/shutdown.tcl": "test {old} {fixed}\n"}, "Deflake shutdown test (#5)")
    subprocess.run(["git", "-C", clone, "fetch", "-q", "origin"], check=True)
    source = _source(fix, "Deflake shutdown test")
    # precheck no longer settles it: it hands back where the change starts.
    base = eligibility.precheck(clone, source, "unstable")
    assert base == _git(Path(clone), "rev-parse", f"{fix}^").strip()
    candidate = eligibility.assess(clone, source, ["9.0"], base=base, history=BranchHistory(clone))
    assert isinstance(candidate, eligibility.Candidate)
    assert candidate.non_product and candidate.branches == {"9.0"}
    product = eligibility.assess(clone, _source(repo["fix"], "Fix shutdown"), ["9.0"],
                                 base=f"{repo['fix']}^", history=BranchHistory(clone))
    assert isinstance(product, eligibility.Candidate) and not product.non_product


def test_docs_only_pr_is_never_a_candidate(repo):
    clone = repo["clone"]
    upstream = Path(clone).parent / "upstream"
    fix = _commit(upstream, {"docs/shutdown.md": "How shutdown works.\n"}, "Document shutdown (#6)")
    subprocess.run(["git", "-C", clone, "fetch", "-q", "origin"], check=True)
    result = eligibility.assess(clone, _source(fix, "Document shutdown"), ["9.0"],
                                base=f"{fix}^", history=BranchHistory(clone))
    assert result == {"action": "not-eligible", "detail": "changes only docs"}
