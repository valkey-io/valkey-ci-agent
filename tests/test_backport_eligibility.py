"""Daily backport eligibility: history bounds the branches, the model narrows them."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.backport import eligibility
from scripts.backport.eligibility import (
    COMMENT_MARKER,
    Candidate,
    LineOrigin,
    MergedSourcePR,
)
from scripts.backport.registry import load_registry
from scripts.backport.source_plan import prepare_source_change
from scripts.release_notes import code_age
from scripts.release_notes.models import MergedPR


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, text: str, subject: str) -> str:
    (repo / path).write_text(text)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path: Path) -> dict[str, Any]:
    """unstable with two release forks.

    Both branches fork after ``old.c``. ``feat.c`` arrives on unstable later and
    is cherry-picked to 9.1 only. The fix to ``old.c`` is already on 9.0.
    """
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "unstable")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "old.c", "int a;\nint b;\nint c;\n", "base (#1)")
    _git(repo, "branch", "9.0")
    _git(repo, "branch", "9.1")

    feature = _commit(repo, "feat.c", "int f(void) { return 0; }\n", "add feature (#100)")
    fix_feature = _commit(repo, "feat.c", "int f(void) { return 1; }\n", "fix feature (#101)")
    fix_old = _commit(repo, "old.c", "int a;\nint B;\nint c;\n", "fix old (#102)")
    additive = _commit(repo, "more.c", "int m;\n", "add more (#103)")

    _git(repo, "checkout", "-q", "9.1")
    _git(repo, "cherry-pick", feature)
    _git(repo, "checkout", "-q", "9.0")
    _git(repo, "cherry-pick", fix_old)
    _git(repo, "checkout", "-q", "unstable")
    return {
        "path": repo,
        "shas": {101: fix_feature, 102: fix_old, 103: additive},
    }


@pytest.fixture
def clone(upstream: dict[str, Any], tmp_path: Path) -> str:
    dest = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-checkout", str(upstream["path"]), str(dest)],
        check=True, capture_output=True,
    )
    return str(dest)


def _pr(number: int, sha: str) -> MergedSourcePR:
    return MergedSourcePR(
        pr=MergedPR(number=number, title=f"PR {number}", author="dev",
                    url=f"https://x/{number}", merge_commit_sha=sha),
        merged_at="2000-01-01T00:00:00Z",
        source_commit_shas=(sha,),
    )


class TestTraceOrigins:
    def test_fix_to_code_on_every_branch_offers_every_branch(self, clone, upstream):
        # base (#1) predates both forks, but 9.0 already carries #102.
        origins = eligibility.trace_origins(
            clone, _pr(102, upstream["shas"][102]), ["9.1", "9.0"],
        )
        assert origins == (LineOrigin("base (#1)", origins[0].committed_at, ("9.1",)),)

    def test_fix_to_cherry_picked_feature_follows_the_cherry_pick(self, clone, upstream):
        # feat.c never reached 9.0. On 9.1 it has another SHA, found by PR number.
        origins = eligibility.trace_origins(
            clone, _pr(101, upstream["shas"][101]), ["9.1", "9.0"],
        )
        assert [(o.subject, o.branches) for o in origins] == [("add feature (#100)", ("9.1",))]

    def test_pr_already_on_every_branch_offers_none(self, clone, upstream):
        origins = eligibility.trace_origins(clone, _pr(102, upstream["shas"][102]), ["9.0"])
        assert origins is not None
        assert Candidate(_pr(102, upstream["shas"][102]), origins).branches == set()

    def test_branch_cut_after_the_merge_already_has_the_pr(self, clone, upstream):
        # A branch forked from unstable after #101 merged carries its commit
        # under the same SHA, even if mergedAt is later than the commit time.
        repo = upstream["path"]
        _git(repo, "branch", "9.2", upstream["shas"][101])
        dest = Path(clone).parent / "clone-92"
        subprocess.run(["git", "clone", "-q", "--no-checkout", str(repo), str(dest)],
                       check=True, capture_output=True)
        source = MergedSourcePR(
            pr=MergedPR(number=101, title="t", author="a", url="u",
                        merge_commit_sha=upstream["shas"][101]),
            merged_at="2999-01-01T00:00:00Z",
            source_commit_shas=(upstream["shas"][101],),
        )
        origins = eligibility.trace_origins(str(dest), source, ["9.2", "9.1"])
        assert origins is not None
        assert Candidate(source, origins).branches == {"9.1"}

    def test_unreadable_branch_raises_instead_of_offering_nothing(self, clone, upstream):
        # A branch whose history cannot be read must not pass for one that
        # lacks the code.
        with pytest.raises(subprocess.CalledProcessError):
            eligibility.trace_origins(clone, _pr(101, upstream["shas"][101]), ["9.9"])

    def test_unreadable_ancestry_raises(self, clone, upstream, monkeypatch):
        def broken(self, sha):
            raise subprocess.CalledProcessError(129, ["git", "for-each-ref"])

        monkeypatch.setattr(eligibility.BranchHistory, "containing", broken)
        with pytest.raises(subprocess.CalledProcessError):
            eligibility.trace_origins(clone, _pr(101, upstream["shas"][101]), ["9.1"])

    def test_pr_adding_only_new_files_has_no_origins(self, clone, upstream):
        assert eligibility.trace_origins(clone, _pr(103, upstream["shas"][103]), ["9.1"]) == ()

    def test_too_large_to_blame_is_unknown(self, clone, upstream, monkeypatch):
        monkeypatch.setattr(eligibility, "_MAX_TRACED_FILES", 0)
        with pytest.raises(eligibility.OriginsUnknown):
            eligibility.trace_origins(clone, _pr(101, upstream["shas"][101]), ["9.1"])

    @pytest.mark.parametrize(
        ("after", "expected"),
        [
            ("int a;\nif (!a) return;\nint b;\nint c;\n", "middle"),
            ("/* guard */\nint a;\nint b;\nint c;\n", "top"),
            ("int a;\nint b;\nint c;\nfree(a);\n", "end"),
        ],
    )
    def test_insertion_only_fix_is_traced_to_the_code_around_it(
        self, tmp_path, after, expected,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "unstable")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        _commit(repo, "old.c", "int a;\nint b;\nint c;\n", "base (#1)")
        _git(repo, "branch", "9.0")
        fix = _commit(repo, "old.c", after, f"add a check at the {expected} (#2)")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", "--no-checkout", str(repo), str(clone)],
                       check=True, capture_output=True)

        origins = eligibility.trace_origins(str(clone), _pr(2, fix), ["9.0"])
        assert [(o.subject, o.branches) for o in origins] == [("base (#1)", ("9.0",))]

    def test_blame_looks_through_ignored_reformats(self, tmp_path):
        # 9.0 forks before a reformat it never receives. Blame must reach the
        # commit that wrote the line, which 9.0 carries.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "unstable")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        _commit(repo, "old.c", "int  a;\nint  b;\n", "write code (#1)")
        _git(repo, "branch", "9.0")
        reformat = _commit(repo, "old.c", "int a;\nint b;\n", "reformat (#2)")
        _commit(repo, ".git-blame-ignore-revs", f"# formatting\n{reformat}\n", "ignore it (#3)")
        fix = _commit(repo, "old.c", "int a;\nint B;\n", "fix b (#4)")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", "--no-checkout", str(repo), str(clone)],
                       check=True, capture_output=True)

        origins = eligibility.trace_origins(str(clone), _pr(4, fix), ["9.0"])
        assert [(o.subject, o.branches) for o in origins] == [("write code (#1)", ("9.0",))]

    def test_unreadable_merge_commit_is_a_source_change_error(self, clone):
        source = _pr(999, "0" * 40)
        with pytest.raises(eligibility.SourceChangeError):
            eligibility.trace_origins(clone, source, ["9.1"])

    def test_multi_commit_squash_traces_the_complete_change(self, tmp_path):
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git(upstream, "init", "-q", "-b", "main")
        _git(upstream, "config", "user.email", "t@example.com")
        _git(upstream, "config", "user.name", "T")
        _commit(upstream, "value.txt", "one\n", "introduce one (#1)")
        _commit(upstream, "value.txt", "one\ntwo\n", "introduce two (#2)")
        _git(upstream, "branch", "9.0")
        _git(upstream, "checkout", "-q", "-b", "source")
        first = _commit(upstream, "value.txt", "ONE\ntwo\n", "change one")
        second = _commit(upstream, "value.txt", "ONE\nTWO\n", "change two")
        _git(upstream, "checkout", "-q", "main")
        _git(upstream, "merge", "-q", "--squash", "source")
        _git(upstream, "commit", "-q", "-m", "squash change (#10)")
        squash = _git(upstream, "rev-parse", "HEAD")

        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--no-checkout", str(upstream), str(clone)],
            check=True,
            capture_output=True,
        )
        source = MergedSourcePR(
            pr=MergedPR(number=10, title="t", author="a", url="u",
                        merge_commit_sha=squash),
            merged_at="2000-01-01T00:00:00Z",
            source_commit_shas=(first, second),
        )

        origins = eligibility.trace_origins(str(clone), source, ["9.0"])
        assert origins is not None
        assert {(origin.subject, origin.branches) for origin in origins} == {
            ("introduce one (#1)", ("9.0",)),
            ("introduce two (#2)", ("9.0",)),
        }

    def _squash_after_syncing_the_base(self, tmp_path: Path, subject: str) -> tuple[str, MergedSourcePR]:
        """#4644's shape: the PR branch merges the base in, then the base moves
        over a line the PR also changes before the PR is squashed."""
        repo = tmp_path / "upstream"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "unstable")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        _commit(repo, "old.c", "a\nb\nc\nd\ne\n", "base (#1)")
        _git(repo, "branch", "9.0")
        _git(repo, "checkout", "-q", "-b", "source")
        first = _commit(repo, "old.c", "A\nb\nc\nd\ne\n", "fix a")
        _git(repo, "checkout", "-q", "unstable")
        _commit(repo, "other.c", "x\n", "unrelated (#2)")
        _git(repo, "checkout", "-q", "source")
        _git(repo, "merge", "-q", "--no-edit", "unstable")
        sync = _git(repo, "rev-parse", "HEAD")
        second = _commit(repo, "old.c", "A\nb\nc\nd\nE\n", "fix e")
        _git(repo, "checkout", "-q", "unstable")
        _commit(repo, "old.c", "A\nb\nc\nd\ne\n", "same fix to a (#3)")
        _git(repo, "merge", "-q", "--squash", "source")
        _git(repo, "commit", "-q", "-m", subject)
        squash = _git(repo, "rev-parse", "HEAD")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", "--no-checkout", str(repo), str(clone)],
                       check=True, capture_output=True)
        source = MergedSourcePR(
            pr=MergedPR(number=10, title="t", author="a", url="u", merge_commit_sha=squash),
            merged_at="2000-01-01T00:00:00Z",
            source_commit_shas=(first, sync, second),
        )
        # The apply path's exact patch check rejects this squash.
        with pytest.raises(eligibility.SourceChangeError):
            prepare_source_change(str(clone), 10, squash, source.source_commit_shas)
        return str(clone), source

    def test_squash_of_a_branch_that_merged_the_base_is_traced(self, tmp_path):
        clone, source = self._squash_after_syncing_the_base(tmp_path, "fix a and e (#10)")
        origins = eligibility.trace_origins(clone, source, ["9.0"])
        assert [(o.subject, o.branches) for o in origins] == [("base (#1)", ("9.0",))]

    def test_one_parent_merge_sha_that_is_neither_squash_nor_rebase_is_undecided(self, tmp_path):
        # Without GitHub's "(#10)" squash subject, nothing shows the SHA is the whole change.
        clone, source = self._squash_after_syncing_the_base(tmp_path, "fix a and e")
        with pytest.raises(eligibility.SourceChangeError):
            eligibility.trace_origins(clone, source, ["9.0"])


class TestParseVerdicts:
    def _parse(self, verdicts: Any) -> Any:
        return eligibility._parse_verdicts(
            json.dumps({"verdicts": verdicts}), {1: {"9.1", "9.0"}, 2: {"9.0"}},
        )

    def test_keeps_only_offered_branches_newest_first(self):
        parsed = self._parse([
            {"pr": 1, "backport": True, "branches": ["9.0", "8.0", "9.1"], "reason": " r "},
        ])
        assert parsed == {1: eligibility.Verdict(
            True, ("9.1", "9.0"), "r", "low", (("9.1", ""), ("9.0", "")),
        )}

    def test_reads_confidence_and_per_branch_reasons(self):
        parsed = self._parse([
            {"pr": 1, "backport": True, "confidence": "high", "reason": "r",
             "branches": [{"branch": "9.0", "why": " has f() "}, {"branch": "8.0", "why": "x"}, 7]},
        ])
        assert parsed == {1: eligibility.Verdict(True, ("9.0",), "r", "high", (("9.0", "has f()"),))}

    def test_unknown_confidence_is_low(self):
        parsed = self._parse([{"pr": 1, "backport": True, "confidence": "certain", "branches": ["9.0"]}])
        assert parsed is not None and parsed[1].confidence == "low"

    def test_positive_verdict_without_an_offered_branch_is_undecided(self):
        assert self._parse([{"pr": 2, "backport": True, "branches": ["9.1"]}]) == {}
        assert self._parse([{"pr": 2, "backport": True}]) == {}

    def test_negative_verdict_carries_no_branches(self):
        parsed = self._parse([{"pr": 2, "backport": False, "branches": ["9.0"], "reason": "feature"}])
        assert parsed == {2: eligibility.Verdict(False, (), "feature")}

    def test_drops_unknown_duplicate_and_malformed_entries(self):
        parsed = self._parse([
            {"pr": 3, "backport": True, "branches": ["9.0"]},
            {"pr": True, "backport": True, "branches": ["9.0"]},
            {"pr": 1, "backport": "yes", "branches": ["9.0"]},
            {"pr": 2, "backport": False},
            {"pr": 2, "backport": True, "branches": ["9.0"]},
            "junk",
        ])
        assert parsed == {2: eligibility.Verdict(False, (), "")}

    def test_unparseable_output_is_none(self):
        assert eligibility._parse_verdicts("no json here", {1: {"9.0"}}) is None


class TestPrFromNode:
    def _node(self, **overrides: Any) -> dict[str, Any]:
        node: dict[str, Any] = {
            "number": 7, "title": "t", "body": None, "url": "u",
            "mergedAt": "2026-01-01T00:00:00Z", "author": None,
            "mergeCommit": {"oid": "abc"},
            "commits": {
                "nodes": [{"commit": {"oid": "abc"}}],
                "pageInfo": {"hasNextPage": False},
            },
            "projectItems": {"nodes": [
                {"project": {"number": 2, "owner": {"login": "Org"}}},
                {"project": {"number": 3, "owner": {"login": "someone-else"}}},
            ]},
            "comments": {"nodes": [{"body": "hi"}]},
        }
        node.update(overrides)
        return node

    def test_reads_projects_of_the_registry_owner_only(self):
        pr = eligibility._pr_from_node(self._node(), "org")
        assert pr is not None
        assert pr.project_numbers == frozenset({2})
        assert (pr.pr.body, pr.pr.author, pr.already_commented) == ("", "", False)

    def test_detects_an_earlier_eligibility_comment(self):
        node = self._node(comments={"nodes": [
            {"body": f"{COMMENT_MARKER}\nold", "viewerDidAuthor": True},
        ]})
        pr = eligibility._pr_from_node(node, "org")
        assert pr is not None and pr.already_commented

    def test_ignores_a_forged_eligibility_comment(self):
        node = self._node(comments={"nodes": [
            {"body": f"{COMMENT_MARKER}\nforged", "viewerDidAuthor": False},
        ]})
        pr = eligibility._pr_from_node(node, "org")
        assert pr is not None and not pr.already_commented

    def test_unreadable_project_item_leaves_projects_unknown(self):
        pr = eligibility._pr_from_node(self._node(projectItems={"nodes": [None]}), "org")
        assert pr is not None and pr.project_numbers is None

    def test_unlisted_project_page_leaves_projects_unknown(self):
        node = self._node()
        node["projectItems"]["pageInfo"] = {"hasNextPage": True}
        pr = eligibility._pr_from_node(node, "org")
        assert pr is not None and pr.project_numbers is None

    def test_skips_a_node_without_merge_commit(self):
        assert eligibility._pr_from_node(self._node(mergeCommit=None), "org") is None
        assert eligibility._pr_from_node({}, "org") is None

    def test_reads_source_commit_listing_completeness(self):
        node = self._node(commits={
            "nodes": [{"commit": {"oid": "one"}}, {"commit": {"oid": "two"}}],
            "pageInfo": {"hasNextPage": True},
        })
        pr = eligibility._pr_from_node(node, "org")
        assert pr is not None
        assert pr.source_commit_shas == ("one", "two")
        assert pr.source_commits_complete is False

    def test_source_commits_do_not_replace_the_merge_commit(self):
        # Reading a squash-merged PR's branch commits must keep the merge SHA,
        # or the squash looks like a rebase merge and blame reads the wrong diff.
        node = self._node(commits={
            "nodes": [{"commit": {"oid": "one"}}, {"commit": {"oid": "two"}}],
            "pageInfo": {"hasNextPage": False},
        })
        pr = eligibility._pr_from_node(node, "org")
        assert pr is not None
        assert pr.pr.merge_commit_sha == node["mergeCommit"]["oid"]


def test_discover_pages_and_queries_one_utc_day():
    calls: list[dict[str, Any]] = []
    pages = [
        {"search": {"nodes": [{"number": 9, "mergedAt": "x", "mergeCommit": {"oid": "a"}}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}},
        {"search": {"nodes": [{"number": 4, "mergedAt": "x", "mergeCommit": {"oid": "b"}}],
                    "pageInfo": {"hasNextPage": False}}},
    ]

    class Gql:
        def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            calls.append(variables)
            return pages[len(calls) - 1]

    prs = eligibility.discover_merged_prs(
        Gql(), repo="org/core", base_branch="unstable", date="2026-10-12", project_owner="org",
    )
    assert [pr.number for pr in prs] == [4, 9]
    assert calls[0]["q"] == (
        "repo:org/core is:pr is:merged base:unstable "
        "merged:2026-10-12T00:00:00Z..2026-10-12T23:59:59Z"
    )
    assert calls[1]["cursor"] == "c1"


def test_discover_covers_a_window_of_days():
    calls: list[dict[str, Any]] = []

    class Gql:
        def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            calls.append(variables)
            return {"search": {"nodes": [], "pageInfo": {"hasNextPage": False}}}

    eligibility.discover_merged_prs(
        Gql(), repo="org/core", base_branch="unstable", date="2026-03-01",
        project_owner="org", days=3,
    )
    assert calls[0]["q"].endswith("merged:2026-02-27T00:00:00Z..2026-03-01T23:59:59Z")


_REGISTRY = """
repos:
  - repo: org/core
    project_owner: org
    language: c
    branches:
      - branch: "9.0"
        project_number: 10
      - branch: "9.1"
        project_number: 11
"""


def _repo_entry(tmp_path: Path):
    path = tmp_path / "repos.yml"
    path.write_text(_REGISTRY)
    return load_registry(str(path)).get_repo("org/core")


def test_render_comment_names_branches_and_projects(tmp_path):
    body = eligibility.render_comment(_repo_entry(tmp_path), ("9.1", "9.0"), "Fixes a crash.")
    assert body.splitlines()[0] == COMMENT_MARKER
    assert "eligible for backport to `9.1` and `9.0`." in body
    assert "Fixes a crash." in body
    assert "[9.1](https://github.com/orgs/org/projects/11)" in body
    assert "[9.0](https://github.com/orgs/org/projects/10)" in body


def test_render_comment_makes_no_claim_about_history(tmp_path):
    body = eligibility.render_comment(_repo_entry(tmp_path), ("9.0",), "Fixes a crash.")
    assert "introduced before those release branches were cut" not in body
    assert "confirm the defect exists on each branch" in body


def test_render_comment_lists_why_each_branch_is_affected(tmp_path):
    body = eligibility.render_comment(
        _repo_entry(tmp_path), ("9.1", "9.0"), "Fixes a crash.",
        {"9.1": "has dictScan (since 7.0)", "9.0": "same code"},
    )
    assert "- `9.1`: has dictScan (since 7.0)" in body
    assert "- `9.0`: same code" in body


def test_render_comment_bounds_the_model_reason(tmp_path):
    body = eligibility.render_comment(
        _repo_entry(tmp_path), ("9.0",), "Pings @maintainers\n\n## Heading " + "x" * 1000,
    )
    reason = body.splitlines()[3]
    assert reason.startswith("> Pings @\u200bmaintainers ## Heading")
    assert len(reason) <= 410
    assert "\n## Heading" not in body


def test_render_comment_reason_keeps_parentheses():
    assert eligibility._inert_reason("Fixes (rare) overflow in f(x).") == "Fixes (rare) overflow in f(x)."


def test_render_comment_reason_cannot_carry_markup(tmp_path):
    body = eligibility.render_comment(
        _repo_entry(tmp_path), ("9.0",),
        "## [details](https://evil.example/x) ![i](http://e/i.png) <img src=x> `c` www.evil.example",
    )
    quote = body.splitlines()[3]
    assert quote.startswith("> \\[details\\]")
    reason = quote[2:]
    # Every markup character is backslash-escaped, so none opens a construct.
    assert re.search(r"(?<!\\)[\[\]<>!`]", reason) is None
    assert "://" not in reason and "www." not in reason


class _FakeIssue:
    def __init__(self, number: int, posted: list[tuple[int, str]]) -> None:
        self._number = number
        self._posted = posted

    def create_comment(self, body: str) -> None:
        self._posted.append((self._number, body))


@pytest.fixture
def wired(monkeypatch, upstream, tmp_path):
    """run() against the fixture repo with GitHub and the model stubbed."""
    posted: list[tuple[int, str]] = []
    shas = upstream["shas"]
    nodes = [
        # #101 fixes the feature that only 9.1 carries.
        {"number": 101, "mergedAt": "2000-01-01T00:00:00Z",
         "mergeCommit": {"oid": shas[101]},
         "commits": {"nodes": [{"commit": {"oid": shas[101]}}]}},
        # #102 is on the 9.1 project already and has landed on 9.0.
        {"number": 102, "mergedAt": "2000-01-01T00:00:00Z", "mergeCommit": {"oid": shas[102]},
         "commits": {"nodes": [{"commit": {"oid": shas[102]}}]},
         "projectItems": {"nodes": [{"project": {"number": 11, "owner": {"login": "org"}}}]}},
        # #103 adds code only.
        {"number": 103, "mergedAt": "2000-01-01T00:00:00Z",
         "mergeCommit": {"oid": shas[103]},
         "commits": {"nodes": [{"commit": {"oid": shas[103]}}]}},
        # #104 was handled by an earlier run.
        {"number": 104, "mergedAt": "2000-01-01T00:00:00Z", "mergeCommit": {"oid": shas[101]},
         "commits": {"nodes": [{"commit": {"oid": shas[101]}}]},
         "comments": {"nodes": [{"body": COMMENT_MARKER, "viewerDidAuthor": True}]}},
    ]

    class Gql:
        def __init__(self, token: str) -> None:
            pass

        def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            return {"search": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}

    repo = SimpleNamespace(
        default_branch="unstable",
        get_issue=lambda number: _FakeIssue(number, posted),
    )
    monkeypatch.setattr(eligibility, "Github", lambda **_: SimpleNamespace(get_repo=lambda _: repo))
    monkeypatch.setattr(eligibility, "GitHubGraphQLClient", Gql)
    cloned: list[str] = []

    def clone(repo_name: str, branch: str, dest: str, env: dict[str, str]) -> None:
        cloned.append(branch)
        subprocess.run(["git", "clone", "-q", "--no-checkout", str(upstream["path"]), dest],
                       check=True)

    monkeypatch.setattr(eligibility, "clone_target_branch", clone)
    # Every branch is GA, but went GA after these PRs merged, so landing on
    # one is not a maintainer's triage decision.
    after = datetime(2001, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(eligibility, "ga_dates", lambda _, branches: {b: after for b in branches})
    return SimpleNamespace(entry=_repo_entry(tmp_path), posted=posted, cloned=cloned, nodes=nodes)


def _model(verdicts: list[dict[str, Any]], prompts: list[str] | None = None):
    # Verdicts in these tests are confident unless they say otherwise, and
    # cite a failure site every fixture branch has.
    verdicts = [
        {"confidence": "high", **v, "branches": [
            {"branch": b, "failure_site": {"file": "", "symbol": "int a;"}} if isinstance(b, str) else b
            for b in v.get("branches", [])
        ]}
        for v in verdicts
    ]

    def run_fn(prompt: str, **kwargs: Any) -> tuple[str, str, int]:
        assert kwargs["allowed_tools"] == ""
        if prompts is not None:
            prompts.append(prompt)
        return json.dumps({"verdicts": verdicts}), "", 0
    return run_fn


def test_run_comments_only_on_eligible_prs(wired):
    prompts: list[str] = []
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}],
                      prompts),
    )
    assert result["errors"] == []
    assert wired.cloned == ["unstable"]
    assert result["prs"] == {
        "101": {"action": "commented", "branches": ["9.1"], "reason": "Fixes f."},
        "102": {"action": "not-eligible", "detail": "no release branch still needs the change"},
        "103": {"action": "not-eligible", "detail": "only adds new files"},
        "104": {"action": "already-commented"},
    }
    # Only #101 reaches the model, with the origin and its branch.
    payload = json.loads(prompts[0].split("## Pull requests (JSON)\n", 1)[1].split("\n## Output", 1)[0])
    assert [entry["number"] for entry in payload] == [101]
    assert payload[0]["line_origins"][0]["commit"] == "add feature (#100)"
    assert payload[0]["line_origins"][0]["candidate_branches"] == ["9.1"]
    assert len(wired.posted) == 1
    number, body = wired.posted[0]
    assert number == 101 and body.startswith(COMMENT_MARKER) and "`9.1`" in body


def test_run_dry_run_posts_nothing(wired):
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t", dry_run=True,
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "r"}]),
    )
    assert result["prs"]["101"]["action"] == "would-comment"
    assert wired.posted == []


def test_run_negative_verdict_posts_nothing(wired):
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": False, "reason": "Refactor."}]),
    )
    assert result["prs"]["101"] == {"action": "not-eligible", "detail": "Refactor."}
    assert result["errors"] == [] and wired.posted == []


def test_run_missing_verdict_is_an_error_without_a_comment(wired):
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=_model([]),
    )
    assert result["prs"]["101"] == {"action": "undecided"}
    assert result["errors"] == ["#101: no verdict"]
    assert wired.posted == []


def test_run_unreadable_projects_fail_only_that_pr(wired):
    wired.nodes.append({
        "number": 105, "mergedAt": "2000-01-01T00:00:00Z",
        "mergeCommit": {"oid": wired.nodes[0]["mergeCommit"]["oid"]},
        "projectItems": {"nodes": [None]},
    })
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "r"}]),
    )
    assert result["prs"]["105"] == {"action": "undecided"}
    assert result["prs"]["101"]["action"] == "commented"
    assert result["errors"] == ["#105: project items unreadable"]
    assert [number for number, _ in wired.posted] == [101]


def test_run_skips_history_when_every_branch_is_already_queued(wired, monkeypatch):
    wired.nodes[1]["projectItems"]["nodes"].append(
        {"project": {"number": 10, "owner": {"login": "org"}}}
    )
    real_trace_origins = eligibility.trace_origins
    traced: list[int] = []

    def trace(repo_dir, source, branches, **kwargs):
        assert branches
        traced.append(source.number)
        return real_trace_origins(repo_dir, source, branches, **kwargs)

    monkeypatch.setattr(eligibility, "trace_origins", trace)
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": False, "reason": "r"}]),
    )

    assert 102 not in traced
    assert result["prs"]["102"] == {
        "action": "not-eligible",
        "detail": "no release branch still needs the change",
    }


def test_run_source_change_failure_needs_review_without_failing(wired, monkeypatch):
    real_resolve = eligibility.resolve_change_base

    def resolve(repo_dir, source, **kwargs):
        if source.number == 101:
            raise eligibility.SourceChangeError("unreadable merge history")
        return real_resolve(repo_dir, source, **kwargs)

    monkeypatch.setattr(eligibility, "resolve_change_base", resolve)
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=_model([]),
    )

    assert result["prs"]["101"] == {
        "action": "needs-review",
        "detail": "complete merged change unknown: unreadable merge history",
    }
    assert result["errors"] == []


def test_run_origin_cap_preserves_evidence_for_every_branch(wired, monkeypatch):
    monkeypatch.setattr(eligibility, "_MAX_ORIGINS", 1)
    monkeypatch.setattr(eligibility, "trace_origins", lambda *_, **__: (
        LineOrigin("old (#1)", "2000-01-01T00:00:00+00:00", ("9.0",)),
        LineOrigin("new (#2)", "2001-01-01T00:00:00+00:00", ("9.1",)),
    ))
    prompts: list[str] = []
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.0"]}], prompts),
    )
    assert "new (#2)" in prompts[0] and "old (#1)" in prompts[0]
    assert result["prs"]["101"]["action"] == "commented"
    assert result["prs"]["101"]["branches"] == ["9.0"]


def test_parse_git_timestamp_accepts_utc_z_on_python_39():
    assert eligibility._parse_git_timestamp("2026-10-14T20:09:22Z").isoformat() == (
        "2026-10-14T20:09:22+00:00"
    )


def _rebase_merged(tmp_path: Path, reword_tip: bool = False) -> tuple[str, MergedSourcePR]:
    """A two-commit PR replayed onto an advanced main, one commit at a time."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "value.txt", "base\n", "base")
    _git(repo, "branch", "9.0")
    _git(repo, "checkout", "-q", "-b", "source")
    first = _commit(repo, "value.txt", "first\n", "first source commit")
    second = _commit(repo, "other.txt", "second\n", "second source commit")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main.txt", "advance\n", "advance main")
    _git(repo, "checkout", "-q", "source")
    _git(repo, "rebase", "-q", "main")
    if reword_tip:
        _git(repo, "commit", "-q", "--amend", "-m", "reworded on merge")
    rebased_tip = _git(repo, "rev-parse", "HEAD")
    assert rebased_tip != second
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(repo), str(clone)],
                   check=True, capture_output=True)
    source = MergedSourcePR(
        pr=MergedPR(number=10, title="t", author="a", url="u",
                    merge_commit_sha=rebased_tip),
        merged_at="2026-01-01T00:00:00Z",
        source_commit_shas=(first, second),
    )
    return str(clone), source


def test_multi_commit_rebase_traces_the_whole_series(tmp_path):
    # The merge SHA alone only adds other.txt; the series also rewrites value.txt.
    clone, source = _rebase_merged(tmp_path)
    origins = eligibility.trace_origins(clone, source, ["9.0"])
    assert [(o.subject, o.branches) for o in origins] == [("base", ("9.0",))]


def test_rebase_that_does_not_match_the_pr_commits_is_undecided(tmp_path):
    clone, source = _rebase_merged(tmp_path, reword_tip=True)
    with pytest.raises(eligibility.SourceChangeError, match="rebase-merged"):
        eligibility.trace_origins(clone, source, ["9.0"])


def test_run_any_tracing_failure_is_contained_to_that_pr(wired, monkeypatch):
    real_trace_origins = eligibility.trace_origins

    def trace(repo_dir, source, branches, **kwargs):
        if source.number == 103:
            raise subprocess.CalledProcessError(128, ["git", "log"])
        return real_trace_origins(repo_dir, source, branches, **kwargs)

    monkeypatch.setattr(eligibility, "trace_origins", trace)
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "r"}]),
    )
    assert result["prs"]["103"] == {"action": "undecided", "detail": "tracing failed"}
    assert len(result["errors"]) == 1 and result["errors"][0].startswith("#103: tracing failed")
    assert [number for number, _ in wired.posted] == [101]


def test_run_untraceable_pr_needs_review_without_failing(wired, monkeypatch):
    def trace(repo_dir, source, branches, **kwargs):
        raise eligibility.OriginsUnknown("touches more than 40 files")

    monkeypatch.setattr(eligibility, "trace_origins", trace)
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=_model([]),
    )
    assert result["prs"]["101"] == {
        "action": "needs-review", "detail": "could not trace: touches more than 40 files",
    }
    assert result["errors"] == []


def test_run_comments_after_each_batch(wired, monkeypatch):
    monkeypatch.setattr(eligibility, "_BATCH_SIZE", 1)
    monkeypatch.setattr(eligibility, "trace_origins", lambda *_, **__: (
        LineOrigin("old (#1)", "2000-01-01T00:00:00+00:00", ("9.0",)),
    ))
    calls: list[int] = []

    def run_fn(prompt: str, **kwargs: Any) -> tuple[str, str, int]:
        calls.append(len(wired.posted))
        if '"number": 101' in prompt:
            return json.dumps({"verdicts": [
                {"pr": 101, "backport": True, "confidence": "high", "reason": "r",
                 "branches": [{"branch": "9.0", "failure_site": {"file": "old.c", "symbol": "int a;"}}]},
            ]}), "", 0
        return "", "timeout after 900s", 1

    result = eligibility.run(repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=run_fn)
    # #101 is judged, then commented on, before the next batch.
    assert calls[:2] == [0, 1]
    assert result["prs"]["101"]["action"] == "commented"
    assert result["prs"]["103"] == {"action": "undecided"}


def test_run_ignores_a_registered_branch_that_does_not_exist(wired, upstream):
    _git(upstream["path"], "branch", "-D", "9.0")
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "r"}]),
    )
    assert result["errors"] == []
    assert result["released_branches"] == ["9.1"]
    assert result["prs"]["101"]["action"] == "commented"


@pytest.mark.parametrize(("errors", "exits"), [([], False), (["#2: no verdict"], True)])
def test_main_warns_on_needs_review_and_fails_only_on_errors(monkeypatch, caplog, errors, exits):
    monkeypatch.setattr(eligibility, "load_registry", lambda _: SimpleNamespace(get_repo=lambda _: None))
    monkeypatch.setattr(eligibility, "emit_job_summary", lambda _: None)
    monkeypatch.setattr(eligibility, "run", lambda **_: {
        "repo": "org/core", "since": "2026-10-12", "date": "2026-10-12", "dry_run": False,
        "prs": {"1": {"action": "needs-review", "detail": "could not trace: x"}},
        "errors": errors,
    })
    monkeypatch.setattr("sys.argv", ["eligibility", "--date", "2026-10-12", "--target-token", "t"])
    if exits:
        with pytest.raises(SystemExit):
            eligibility.main()
    else:
        eligibility.main()
    assert any(
        r.levelname == "WARNING" and "1 PR(s) need manual review: #1: could not trace: x" in r.getMessage()
        for r in caplog.records
    )


def test_run_comments_only_on_high_confidence(wired):
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "confidence": "medium",
                        "branches": ["9.1"], "reason": "Maybe fixes f."}]),
    )
    assert result["prs"]["101"] == {
        "action": "not-commented", "confidence": "medium",
        "branches": ["9.1"], "reason": "Maybe fixes f.",
    }
    assert result["errors"] == [] and wired.posted == []
    assert "| #101 | not-commented (medium confidence) | 9.1 |" in eligibility._summary(result)


def test_run_gives_the_model_branch_evidence_and_linked_issues(wired):
    wired.nodes[0]["closingIssuesReferences"] = {"nodes": [{"number": 9, "title": "f breaks", "body": "b"}]}
    prompts: list[str] = []
    eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t", dry_run=True,
        run_fn=_model([{"pr": 101, "backport": False, "reason": "r"}], prompts),
    )
    payload = json.loads(prompts[0].split("## Pull requests (JSON)\n", 1)[1].split("\n## Output", 1)[0])
    assert payload[0]["linked_issues"] == ["#9 f breaks\nb"]
    assert payload[0]["branch_evidence"]["9.1"]["modified_files_present"] == ["feat.c"]
    assert payload[0]["branch_evidence"]["9.1"]["patch_applies_cleanly"] is True


def test_prompt_asks_for_precision():
    prompt = eligibility._PROMPT_TEMPLATE
    assert "when the evidence is thin, answer true" not in prompt.lower()
    assert "confidence" in prompt and "a fix for a flaky test" in prompt.lower()


@pytest.mark.parametrize(("value", "expected"), [
    ("4849,4880", [4849, 4880]), (" #4644 , 4849 ", [4644, 4849]), ("4849 4880", [4849, 4880]),
])
def test_parse_pr_numbers(value, expected):
    assert eligibility.parse_pr_numbers(value) == expected


@pytest.mark.parametrize("value", ["", "4849,abc", "0", "4849;rm -rf /"])
def test_parse_pr_numbers_rejects_anything_else(value):
    with pytest.raises(ValueError):
        eligibility.parse_pr_numbers(value)


def test_fetch_merged_prs_reports_a_number_that_is_not_a_pr():
    # GitHub fails the whole query for one number that is not a pull request.
    class Gql:
        def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "p99999999:" in query:
                raise RuntimeError(
                    "GraphQL errors: Could not resolve to a PullRequest with the number of 99999999."
                )
            return {"repository": {
                "p7": {"number": 7, "mergedAt": "x", "mergeCommit": {"oid": "a"}, "baseRefName": "unstable"},
            }}

    prs, problems = eligibility.fetch_merged_prs(
        Gql(), repo="org/core", base_branch="unstable", numbers=[7, 99999999], project_owner="org",
    )
    assert [pr.number for pr in prs] == [7]
    assert problems == ["#99999999: not a merged pull request"]


def test_fetch_merged_prs_other_graphql_errors_still_raise():
    class Gql:
        def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("GraphQL errors: rate limited")

    with pytest.raises(RuntimeError, match="rate limited"):
        eligibility.fetch_merged_prs(
            Gql(), repo="org/core", base_branch="unstable", numbers=[7], project_owner="org",
        )


def test_fetch_merged_prs_reports_unusable_numbers():
    calls: list[tuple[str, dict[str, Any]]] = []

    class Gql:
        def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            calls.append((query, variables))
            return {"repository": {
                "p7": {"number": 7, "mergedAt": "x", "mergeCommit": {"oid": "a"}, "baseRefName": "unstable"},
                "p8": {"number": 8, "mergedAt": None, "mergeCommit": None, "baseRefName": "unstable"},
                "p9": {"number": 9, "mergedAt": "x", "mergeCommit": {"oid": "b"}, "baseRefName": "9.0"},
                "p10": None,
            }}

    prs, problems = eligibility.fetch_merged_prs(
        Gql(), repo="org/core", base_branch="unstable", numbers=[10, 9, 8, 7, 7], project_owner="org",
    )
    assert [pr.number for pr in prs] == [7]
    assert problems == [
        "#8: not a merged pull request",
        "#9: merged into 9.0, not unstable",
        "#10: not a merged pull request",
    ]
    query, variables = calls[0]
    assert variables == {"owner": "org", "name": "core"}
    assert "p7: pullRequest(number: 7) { ...PrFields }" in query and "fragment PrFields" in query


def test_run_evaluates_named_prs_instead_of_a_date_window(wired, monkeypatch):
    by_number = {node["number"]: node for node in wired.nodes}
    queries: list[str] = []

    class Gql:
        def __init__(self, token: str) -> None:
            pass

        def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            queries.append(query)
            assert "search(" not in query
            return {"repository": {
                f"p{n}": {**by_number[n], "baseRefName": "unstable"} for n in (101, 104)
            }}

    monkeypatch.setattr(eligibility, "GitHubGraphQLClient", Gql)
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t", pr_numbers=[104, 101],
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}]),
    )
    assert len(queries) == 1
    assert result["requested_prs"] == [101, 104]
    # The marker comment still keeps a named PR from being commented on twice.
    assert result["prs"] == {
        "101": {"action": "commented", "branches": ["9.1"], "reason": "Fixes f."},
        "104": {"action": "already-commented"},
    }
    assert [number for number, _ in wired.posted] == [101]
    assert eligibility._summary(result).startswith("## Backport eligibility for org/core, PRs #101, #104")


def test_main_passes_named_prs(monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(eligibility, "load_registry", lambda _: SimpleNamespace(get_repo=lambda _: None))
    monkeypatch.setattr(eligibility, "emit_job_summary", lambda _: None)

    def fake_run(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"repo": "org/core", "since": "x", "date": "x", "requested_prs": [4849],
                "dry_run": True, "prs": {}, "errors": []}

    monkeypatch.setattr(eligibility, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["eligibility", "--prs", "4849", "--target-token", "t", "--dry-run"])
    eligibility.main()
    assert seen["pr_numbers"] == [4849] and seen["dry_run"] is True


def test_pr_on_the_open_backport_sweep_counts_as_landed(clone, upstream):
    # #101's fix is cherry-picked to the 9.1 sweep branch, not yet merged to 9.1.
    repo = upstream["path"]
    _git(repo, "checkout", "-q", "-b", "agent/backport/sweep/9.1", "9.1")
    _git(repo, "cherry-pick", upstream["shas"][101])
    _git(repo, "commit", "-q", "--amend", "-m", "fix feature (#101)")
    _git(repo, "checkout", "-q", "unstable")
    dest = Path(clone).parent / "clone-sweep"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(repo), str(dest)],
                   check=True, capture_output=True)
    history = eligibility.BranchHistory(str(dest))
    assert history.present("9.1", {101: "2000-01-01T00:00:00Z"}) == {101}
    assert history.present("9.0", {101: "2000-01-01T00:00:00Z"}) == set()
    origins = eligibility.trace_origins(str(dest), _pr(101, upstream["shas"][101]), ["9.1"])
    assert Candidate(_pr(101, upstream["shas"][101]), origins).branches == set()


def test_parse_reads_failure_sites():
    parsed = eligibility._parse_verdicts(json.dumps({"verdicts": [
        {"pr": 1, "backport": True, "confidence": "high", "branches": [
            {"branch": "9.1", "why": "w", "failure_site": {"file": "src/expire.c", "symbol": "serverAssert(t >= 0)"}},
            {"branch": "9.0", "failure_site": "not an object"},
        ]},
    ]}), {1: {"9.1", "9.0"}})
    assert parsed is not None
    assert parsed[1].sites == (("9.1", "src/expire.c", "serverAssert(t >= 0)"),)


def _verdict(*sites: tuple[str, str, str]) -> eligibility.Verdict:
    branches = tuple(dict.fromkeys(b for b, _, _ in sites)) or ("9.1", "9.0")
    return eligibility.Verdict(True, branches, "r", "high", tuple((b, "") for b in branches), sites)


def test_verify_sites_drops_a_branch_without_the_failure_site(clone):
    # feat.c (and f) reached 9.1 by cherry-pick; 9.0 never had it.
    verdict = eligibility.verify_sites(clone, _verdict(
        ("9.1", "feat.c", "int f(void)"), ("9.0", "feat.c", "int f(void)"),
    ))
    assert verdict.branches == ("9.1",) and verdict.dropped == ("9.0",)
    assert verdict.confidence == "high"


def test_verify_sites_with_no_site_left_is_never_commented(clone):
    verdict = eligibility.verify_sites(clone, _verdict(("9.0", "old.c", "serverAssert(t >= 0)")))
    assert verdict.branches == () and verdict.confidence == "low"


def test_verify_sites_unchecked_site_downgrades_to_medium(clone):
    verdict = eligibility.verify_sites(clone, _verdict(("9.1", "feat.c", ""), ("9.0", "old.c", "int a;")))
    assert verdict.branches == ("9.1", "9.0") and verdict.confidence == "medium"


def test_verify_sites_falls_back_to_the_tree_for_a_moved_file(clone):
    # The model may name the development branch's path for a file a release
    # branch keeps elsewhere; the site is then searched for in the whole tree.
    verdict = eligibility.verify_sites(clone, _verdict(("9.0", "src/renamed.c", "int c;")))
    assert verdict.branches == ("9.0",)


def test_verify_sites_ignores_whitespace(clone):
    # 7.2 predates the clang-format reformat, so a site quoted from current
    # code is spaced differently there; it is still the same code.
    for path in ("feat.c", ""):
        verdict = eligibility.verify_sites(clone, _verdict(("9.1", path, "int f( void ){return 0;}")))
        assert verdict.branches == ("9.1",), path
    assert eligibility.verify_sites(clone, _verdict(("9.1", "feat.c", "int f(int)"))).branches == ()


@pytest.mark.parametrize("symbol", ["}", "0", "->", "int", "  ;  ", "a b", ".*"])
def test_verify_sites_a_trivial_site_proves_nothing(clone, symbol):
    # A site that matches nearly any tree is unchecked: kept, but never commented.
    verdict = eligibility.verify_sites(clone, _verdict(("9.0", "old.c", symbol)))
    assert verdict.branches == ("9.0",) and verdict.confidence == "medium"


def test_verify_sites_patterns_are_fixed_strings(clone):
    # A regex or option-looking site matches only itself, literally.
    for symbol in ("--all-match", "int [a-c];"):
        assert eligibility.verify_sites(clone, _verdict(("9.0", "", symbol))).branches == ()


def test_run_does_not_comment_when_the_failure_site_is_missing(wired):
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "reason": "crash", "branches": [
            {"branch": "9.1", "failure_site": {"file": "feat.c", "symbol": "serverAssert(t >= 0)"}},
        ]}]),
    )
    assert result["prs"]["101"]["action"] == "not-commented"
    assert result["prs"]["101"]["failure_site_missing_on"] == ["9.1"]
    assert wired.posted == []


def test_run_pr_maintainers_queued_on_a_ga_branch_is_triaged(wired, monkeypatch):
    # #4880's shape: queued for every GA branch but one (here 9.0). The branch
    # maintainers left out (9.1) is their decision too, so the PR is settled
    # before the model and never sent to it.
    wired.nodes[0]["projectItems"] = {"nodes": [{"project": {"number": 10, "owner": {"login": "org"}}}]}
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(eligibility, "ga_dates", lambda *_: {"9.0": epoch, "9.1": epoch})
    prompts: list[str] = []
    model = _model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}])

    def run_fn(prompt, **kwargs):
        prompts.append(prompt)
        return model(prompt, **kwargs)

    result = eligibility.run(repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=run_fn)
    assert result["prs"]["101"] == {"action": "triaged", "triaged_on": ["9.0"]}
    assert result["errors"] == [] and wired.posted == []
    assert not any('"number": 101' in prompt for prompt in prompts)
    assert "maintainers took it to 9.0" in eligibility._summary(result)


def test_run_queue_on_a_pre_ga_branch_is_not_triage(wired, monkeypatch):
    # Every PR lands on a pre-GA branch (here 9.0); that says nothing about
    # backports, so the GA branch 9.1 is still suggested.
    wired.nodes[0]["projectItems"] = {"nodes": [{"project": {"number": 10, "owner": {"login": "org"}}}]}
    monkeypatch.setattr(eligibility, "ga_dates", lambda *_: {"9.1": datetime(2001, 1, 1, tzinfo=timezone.utc)})
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}]),
    )
    assert result["prs"]["101"]["action"] == "commented"


def test_run_suggests_a_pre_ga_branch(wired, monkeypatch):
    # A registered branch with only an RC is still a release branch that can
    # miss a fix merged to the development branch after it was cut.
    monkeypatch.setattr(eligibility, "ga_dates", lambda *_: {})
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}]),
    )
    assert result["released_branches"] == ["9.1", "9.0"]
    assert result["prs"]["101"] == {
        "action": "commented", "branches": ["9.1"], "reason": "Fixes f.",
    }
    assert [number for number, _ in wired.posted] == [101]


def test_run_pr_landed_on_a_ga_branch_is_triaged(wired, monkeypatch, upstream):
    # #102 already landed on 9.0; with 9.0 GA that is a maintainer decision.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(eligibility, "ga_dates", lambda *_: {"9.0": epoch, "9.1": epoch})
    wired.nodes[1]["projectItems"] = {"nodes": []}
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": False, "reason": "r"},
                       {"pr": 102, "backport": True, "branches": ["9.1"], "reason": "Fixes old."}]),
    )
    assert result["prs"]["102"]["action"] == "triaged"
    assert result["prs"]["102"]["triaged_on"] == ["9.0"]
    assert [number for number, _ in wired.posted] == []


def test_prompt_asks_for_failure_sites_on_the_named_branches():
    prompt = eligibility._PROMPT_TEMPLATE
    assert "failure_site" in prompt and "not on the development branch" in prompt


def test_run_merge_commit_not_on_the_development_branch_is_not_on_base(wired, upstream, monkeypatch):
    # #4850's shape: GitHub says merged, but unstable was reset past the commit.
    repo = upstream["path"]
    _git(repo, "checkout", "-q", "-b", "abandoned")
    orphan = _commit(repo, "old.c", "int a;\nint b;\nint x;\n", "lost change (#106)")
    _git(repo, "checkout", "-q", "unstable")

    def no_triage(*args: Any, **kwargs: Any) -> list[str]:
        raise AssertionError("history must not be asked about a commit off the branch")

    monkeypatch.setattr(eligibility, "triaged_branches", no_triage)
    wired.nodes[:] = [
        {"number": 106, "mergedAt": "2000-01-01T00:00:00Z", "mergeCommit": {"oid": orphan},
         "commits": {"nodes": [{"commit": {"oid": orphan}}]}},
        {"number": 107, "mergedAt": "2000-01-01T00:00:00Z", "mergeCommit": {"oid": "f" * 40},
         "commits": {"nodes": [{"commit": {"oid": "f" * 40}}]}},
    ]
    result = eligibility.run(repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=_model([]))
    assert result["prs"]["106"]["action"] == "not-on-base"
    assert result["prs"]["107"]["action"] == "not-on-base"
    assert result["errors"] == []


def _test_only_pr(wired, upstream) -> None:
    """Replace the PRs with #108, a deflake of a test 9.1 carries."""
    repo = upstream["path"]
    (repo / "tests").mkdir()
    added = _commit(repo, "tests/t.tcl", "test {x} {\n    after 10\n}\n", "add test (#107)")
    _git(repo, "checkout", "-q", "9.1")
    _git(repo, "cherry-pick", added)
    _git(repo, "checkout", "-q", "unstable")
    deflake = _commit(repo, "tests/t.tcl", "test {x} {\n    wait_for_ok\n}\n", "deflake (#108)")
    wired.nodes[:] = [{"number": 108, "mergedAt": "2000-01-01T00:00:00Z",
                       "mergeCommit": {"oid": deflake},
                       "commits": {"nodes": [{"commit": {"oid": deflake}}]}}]


def test_run_judges_a_test_only_pr_but_never_comments(wired, upstream):
    _test_only_pr(wired, upstream)
    prompts: list[str] = []
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 108, "backport": True, "branches": ["9.1"],
                        "reason": "The test flakes on 9.1."}], prompts),
    )
    assert any('"number": 108' in prompt for prompt in prompts)
    entry = result["prs"]["108"]
    assert entry["action"] == "not-commented"
    assert entry["confidence"] == "medium" and entry["branches"] == ["9.1"]
    assert result["errors"] == [] and wired.posted == []


def test_run_test_only_pr_maintainers_queued_is_triaged(wired, upstream, monkeypatch):
    _test_only_pr(wired, upstream)
    wired.nodes[0]["projectItems"] = {"nodes": [{"project": {"number": 11, "owner": {"login": "org"}}}]}
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(eligibility, "ga_dates", lambda *_: {"9.0": epoch, "9.1": epoch})
    result = eligibility.run(repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=_never_called)
    assert result["prs"]["108"] == {"action": "triaged", "triaged_on": ["9.1"]}


def test_run_docs_only_pr_is_not_eligible_without_the_model(wired, upstream):
    docs = _commit(upstream["path"], "README.md", "# Valkey\n", "docs (#109)")
    wired.nodes[:] = [{"number": 109, "mergedAt": "2000-01-01T00:00:00Z",
                       "mergeCommit": {"oid": docs},
                       "commits": {"nodes": [{"commit": {"oid": docs}}]}}]
    result = eligibility.run(repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=_never_called)
    assert result["prs"]["109"] == {"action": "not-eligible", "detail": "changes only docs"}


def test_reads_labels_of_the_pr_and_its_linked_issues():
    node = TestPrFromNode()._node(
        labels={"nodes": [{"name": "bug"}]},
        closingIssuesReferences={"nodes": [
            {"number": 4764, "title": "t", "body": "b",
             "labels": {"nodes": [{"name": "bug"}, {"name": "performance"}]}},
        ]},
    )
    pr = eligibility._pr_from_node(node, "org")
    assert pr is not None and pr.labels == ("bug", "performance")


@pytest.mark.parametrize(("labels", "body"), [
    ({"nodes": [{"name": "performance"}]}, "Fixes a stall."),
    ({"nodes": []}, "A throughput-under-saturation trade-off: fewer stalls, more latency."),
])
def test_run_caps_performance_or_tradeoff_fixes_at_medium(wired, labels, body):
    wired.nodes[0]["labels"] = labels
    wired.nodes[0]["body"] = body
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}]),
    )
    assert result["prs"]["101"]["action"] == "not-commented"
    assert result["prs"]["101"]["confidence"] == "medium"
    assert wired.posted == []




# -- Judging each PR once -------------------------------------------------


def _prior_file(tmp_path: Path, result: dict[str, Any], name: str = "r.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(result))
    return path


def _first_run(wired, tmp_path: Path, *, dry_run: bool, verdict: dict[str, Any]) -> Path:
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t", dry_run=dry_run,
        run_fn=_model([verdict]),
    )
    assert result["version"] == eligibility.RESULT_VERSION
    assert result["released_branches"] == ["9.1", "9.0"]
    return _prior_file(tmp_path, result)


def _never_called(prompt: str, **kwargs: Any) -> tuple[str, str, int]:
    raise AssertionError("the model was asked again")


def test_run_does_not_judge_a_pr_an_earlier_run_settled(wired, tmp_path):
    path = _first_run(wired, tmp_path, dry_run=False, verdict={
        "pr": 101, "backport": True, "confidence": "medium", "branches": ["9.1"], "reason": "maybe",
    })
    prior = eligibility.load_prior_results([path], "org/core")
    result = eligibility.run(
        repo_entry=wired.entry, date="2026-10-13", token="t", run_fn=_never_called, prior=prior,
    )
    entry = result["prs"]["101"]
    assert entry["action"] == "not-commented" and entry["confidence"] == "medium"
    assert entry["judged_on"] == json.loads(path.read_text())["judged_at"][:10]
    assert result["errors"] == [] and wired.posted == []
    # The copy is itself reusable by the run after.
    again = eligibility.load_prior_results([_prior_file(tmp_path, result, "s.json")], "org/core")
    assert again[101].result["action"] == "not-commented"


def test_run_judges_again_when_the_merge_commit_changed(wired, tmp_path):
    path = _first_run(wired, tmp_path, dry_run=False, verdict={
        "pr": 101, "backport": False, "reason": "Refactor."})
    data = json.loads(path.read_text())
    data["merge_shas"]["101"] = "0" * 40
    prior = eligibility.load_prior_results([_prior_file(tmp_path, data)], "org/core")
    prompts: list[str] = []
    eligibility.run(
        repo_entry=wired.entry, date="2026-10-13", token="t", prior=prior,
        run_fn=_model([{"pr": 101, "backport": False, "reason": "r"}], prompts),
    )
    assert len(prompts) == 1


def test_run_judges_again_when_the_released_branches_changed(wired, tmp_path):
    path = _first_run(wired, tmp_path, dry_run=False, verdict={
        "pr": 101, "backport": False, "reason": "Refactor."})
    data = json.loads(path.read_text())
    data["released_branches"] = ["9.0"]
    prior = eligibility.load_prior_results([_prior_file(tmp_path, data)], "org/core")
    prompts: list[str] = []
    eligibility.run(
        repo_entry=wired.entry, date="2026-10-13", token="t", prior=prior,
        run_fn=_model([{"pr": 101, "backport": False, "reason": "r"}], prompts),
    )
    assert len(prompts) == 1


def test_a_dry_run_suggestion_is_commented_by_the_next_live_run(wired, tmp_path):
    path = _first_run(wired, tmp_path, dry_run=True, verdict={
        "pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."})
    prior = eligibility.load_prior_results([path], "org/core")
    # Another dry run reuses it.
    dry = eligibility.run(repo_entry=wired.entry, date="2026-10-13", token="t", dry_run=True,
                          run_fn=_never_called, prior=prior)
    assert dry["prs"]["101"]["action"] == "would-comment"
    # A live run judges it again, and comments.
    live = eligibility.run(
        repo_entry=wired.entry, date="2026-10-13", token="t", prior=prior,
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}]),
    )
    assert live["prs"]["101"]["action"] == "commented"
    assert [number for number, _ in wired.posted] == [101]


def test_run_judges_an_undecided_pr_again(wired, tmp_path):
    first = eligibility.run(repo_entry=wired.entry, date="2026-10-12", token="t", run_fn=_model([]))
    assert first["prs"]["101"]["action"] == "undecided"
    prior = eligibility.load_prior_results([_prior_file(tmp_path, first)], "org/core")
    prompts: list[str] = []
    eligibility.run(
        repo_entry=wired.entry, date="2026-10-13", token="t", prior=prior,
        run_fn=_model([{"pr": 101, "backport": False, "reason": "r"}], prompts),
    )
    assert len(prompts) == 1


def test_named_prs_are_judged_afresh(wired, tmp_path, monkeypatch):
    path = _first_run(wired, tmp_path, dry_run=False, verdict={
        "pr": 101, "backport": False, "reason": "Refactor."})
    prior = eligibility.load_prior_results([path], "org/core")
    monkeypatch.setattr(eligibility, "fetch_merged_prs", lambda *_, **__: (
        [eligibility._pr_from_node(wired.nodes[0], "org")], []))
    prompts: list[str] = []
    eligibility.run(
        repo_entry=wired.entry, date="2026-10-13", token="t", prior=prior, pr_numbers=[101],
        run_fn=_model([{"pr": 101, "backport": False, "reason": "r"}], prompts),
    )
    assert len(prompts) == 1


def test_load_prior_results_skips_other_versions_repos_and_garbage(tmp_path):
    good = {"version": eligibility.RESULT_VERSION, "repo": "org/core", "judged_at": "2026-10-12T01:00:00+00:00",
            "dry_run": False, "released_branches": ["9.1"],
            "prs": {"7": {"action": "not-eligible"}}, "merge_shas": {"7": "a" * 40}}
    newer = {**good, "judged_at": "2026-10-13T01:00:00+00:00", "prs": {"7": {"action": "triaged"}}}
    paths = [
        _prior_file(tmp_path, newer, "newer.json"),
        _prior_file(tmp_path, good, "good.json"),
        _prior_file(tmp_path, {**good, "version": None, "prs": {"8": {"action": "not-eligible"}}}, "old.json"),
        _prior_file(tmp_path, {**good, "repo": "org/other", "prs": {"9": {"action": "x"}}}, "other.json"),
    ]
    (tmp_path / "broken.json").write_text("{not json")
    prior = eligibility.load_prior_results([*paths, tmp_path / "broken.json"], "org/core")
    assert set(prior) == {7}
    assert prior[7].result["action"] == "triaged" and prior[7].judged_on == "2026-10-13"


def test_annotations_escape_untrusted_text_and_skip_reused_prs():
    lines = eligibility.workflow_annotations({"prs": {
        "1": {"action": "commented", "branches": ["9.1"], "reason": "50% crash\n::error::x"},
        "2": {"action": "needs-review", "detail": "could not trace"},
        "3": {"action": "not-eligible", "detail": "refactor"},
        "4": {"action": "commented", "branches": ["9.0"], "reason": "r", "judged_on": "2026-10-12"},
    }})
    assert lines == [
        "::notice title=Backport suggested for #1::commented for 9.1: 50%25 crash%0A::error::x",
        "::warning title=Backport eligibility #2::needs-review: could not trace",
    ]


def test_main_writes_output_and_reads_prior_results(monkeypatch, tmp_path, capsys):
    prior_dir = tmp_path / "prior" / "123"
    prior_dir.mkdir(parents=True)
    (prior_dir / "backport-eligibility-result.json").write_text(json.dumps({
        "version": eligibility.RESULT_VERSION, "repo": "valkey-io/valkey",
        "judged_at": "2026-10-12T01:00:00+00:00", "prs": {"5": {"action": "not-eligible"}},
        "merge_shas": {"5": "b" * 40},
    }))
    seen: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"repo": "valkey-io/valkey", "since": "2026-10-06", "date": "2026-10-12",
                "dry_run": False, "prs": {"6": {"action": "commented", "branches": ["9.1"], "reason": "r"}},
                "errors": []}

    monkeypatch.setattr(eligibility, "load_registry", lambda _: SimpleNamespace(get_repo=lambda _: None))
    monkeypatch.setattr(eligibility, "emit_job_summary", lambda _: None)
    monkeypatch.setattr(eligibility, "run", fake_run)
    out = tmp_path / "result.json"
    monkeypatch.setattr("sys.argv", ["eligibility", "--date", "2026-10-12", "--target-token", "t",
                                     "--prior-results", str(tmp_path / "prior"), "--output", str(out)])
    eligibility.main()
    assert set(seen["prior"]) == {5} and seen["days"] == 7
    assert json.loads(out.read_text())["prs"]["6"]["action"] == "commented"
    assert "::notice title=Backport suggested for #6::commented for 9.1: r" in capsys.readouterr().out


def test_a_deleted_comment_is_not_posted_again(wired, tmp_path):
    first = eligibility.run(
        repo_entry=wired.entry, date="2026-10-12", token="t",
        run_fn=_model([{"pr": 101, "backport": True, "branches": ["9.1"], "reason": "Fixes f."}]),
    )
    assert first["prs"]["101"]["action"] == "commented"
    # The next run sees the marker; then a maintainer deletes the comment.
    wired.nodes[0]["comments"] = {"nodes": [{"body": COMMENT_MARKER, "viewerDidAuthor": True}]}
    second = eligibility.run(repo_entry=wired.entry, date="2026-10-13", token="t", run_fn=_never_called)
    assert second["prs"]["101"] == {"action": "already-commented"}
    del wired.nodes[0]["comments"]
    prior = eligibility.load_prior_results(
        [_prior_file(tmp_path, first, "1.json"), _prior_file(tmp_path, second, "2.json")], "org/core")
    third = eligibility.run(repo_entry=wired.entry, date="2026-10-14", token="t",
                            run_fn=_never_called, prior=prior)
    assert third["prs"]["101"]["action"] == "commented" and "judged_on" in third["prs"]["101"]
    assert [number for number, _ in wired.posted] == [101]
