"""Property-based tests for scripts.backport.pr_creator.BackportPRCreator."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.backport.models import BackportPRContext, CherryPickResult, ResolutionResult
from scripts.backport.pr_creator import (
    BackportPRCreator,
    build_pull_create_head_ref,
    build_pull_search_head_ref,
    create_pull_from_push_repo,
    pull_matches_push_repo,
)
from scripts.backport.utils import build_branch_name

# ── Shared strategies ─────────────────────────────────────────────────

_safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=200,
)

_sha_strategy = st.text(
    alphabet="0123456789abcdef",
    min_size=7,
    max_size=40,
)

_pr_context_strategy = st.builds(
    BackportPRContext,
    source_pr_number=st.integers(min_value=1, max_value=999_999),
    source_pr_title=_safe_text,
    source_pr_url=st.from_regex(r"https://github\.com/[a-z]+/[a-z]+/pull/[0-9]+", fullmatch=True),
    source_pr_diff=_safe_text,
    target_branch=_safe_text,
    commits=st.lists(_sha_strategy, min_size=1, max_size=5),
)

_resolved_result_strategy = st.builds(
    ResolutionResult,
    path=st.from_regex(r"src/[a-z_]+\.[ch]", fullmatch=True),
    resolved_content=_safe_text,
    resolution_summary=_safe_text,
)

_unresolved_result_strategy = st.builds(
    ResolutionResult,
    path=st.from_regex(r"src/[a-z_]+\.[ch]", fullmatch=True),
    resolved_content=st.none(),
    resolution_summary=_safe_text,
)




class TestPRBodyCompletenessProperty:
    """

    For any BackportPRContext and list of ResolutionResults (including cases
    where some files were resolved and some were not), the generated PR body
    should contain: a link to the source PR, the list of cherry-picked commit
    SHAs, whether conflicts were encountered, the resolution method for each
    file, a per-file summary for each LLM-resolved file, and a disclaimer
    about LLM-resolved conflicts requiring human review (when any file was
    LLM-resolved).
    """

    @given(
        context=_pr_context_strategy,
        resolved=st.lists(_resolved_result_strategy, min_size=1, max_size=5),
        unresolved=st.lists(_unresolved_result_strategy, min_size=0, max_size=3),
    )
    @settings(max_examples=100, deadline=None)
    def test_body_with_conflicts_and_mixed_results(
        self,
        context: BackportPRContext,
        resolved: list[ResolutionResult],
        unresolved: list[ResolutionResult],
    ) -> None:
        """When conflicts occurred and some files were LLM-resolved, the body
        must contain all required sections including the human review disclaimer."""
        all_results = resolved + unresolved
        body = BackportPRCreator.build_pr_body(
            context, had_conflicts=True, resolution_results=all_results,
        )

        # Source PR link
        assert context.source_pr_url in body

        # Cherry-picked commit SHAs
        for sha in context.commits:
            assert sha in body

        # Conflict status indicated
        assert "conflict" in body.lower()

        # Per-file resolution details
        for result in all_results:
            assert result.path in body
            assert result.resolution_summary in body

        # Human review disclaimer (at least one file was LLM-resolved)
        assert "human review" in body.lower()

    @given(context=_pr_context_strategy)
    @settings(max_examples=100, deadline=None)
    def test_body_without_conflicts(
        self,
        context: BackportPRContext,
    ) -> None:
        """When cherry-pick was clean, body still has source link and commits."""
        body = BackportPRCreator.build_pr_body(
            context, had_conflicts=False, resolution_results=None,
        )

        # Source PR link
        assert context.source_pr_url in body

        # Cherry-picked commit SHAs
        for sha in context.commits:
            assert sha in body

        # No conflict markers section — but conflict status is still mentioned
        assert "conflict" in body.lower()

        # No human review disclaimer when no LLM resolution
        assert "Human Review Required" not in body

    @given(
        context=_pr_context_strategy,
        unresolved=st.lists(_unresolved_result_strategy, min_size=1, max_size=5),
    )
    @settings(max_examples=100, deadline=None)
    def test_body_with_all_unresolved(
        self,
        context: BackportPRContext,
        unresolved: list[ResolutionResult],
    ) -> None:
        """When all files are unresolved, no human review disclaimer is needed."""
        body = BackportPRCreator.build_pr_body(
            context, had_conflicts=True, resolution_results=unresolved,
        )

        # Source PR link
        assert context.source_pr_url in body

        # Cherry-picked commit SHAs
        for sha in context.commits:
            assert sha in body

        # Per-file details present
        for result in unresolved:
            assert result.path in body
            assert result.resolution_summary in body

        # No human review disclaimer when no file was LLM-resolved
        assert "human review" not in body.lower()


def test_build_pr_body_includes_checklist_and_plain_status_labels() -> None:
    context = BackportPRContext(
        source_pr_number=123,
        source_pr_title="Fix failover edge case",
        source_pr_url="https://github.com/owner/repo/pull/123",
        source_pr_diff="diff",
        target_branch="8.1",
        commits=["abc1234"],
    )
    results = [
        ResolutionResult(
            path="src/server.c",
            resolved_content="resolved",
            resolution_summary="Applied the null check from the source branch.",
        )
    ]

    body = BackportPRCreator.build_pr_body(
        context,
        had_conflicts=True,
        resolution_results=results,
    )

    assert "Reviewer Checklist" in body
    assert "Human Review Required" in body
    assert "Resolved automatically" in body
    assert "✅" not in body
    assert "❌" not in body


def test_automatic_resolution_does_not_get_llm_disclaimer() -> None:
    context = BackportPRContext(
        source_pr_number=123,
        source_pr_title="Whitespace-only conflict",
        source_pr_url="https://github.com/owner/repo/pull/123",
        source_pr_diff="diff",
        target_branch="8.1",
        commits=["abc1234"],
    )
    results = [
        ResolutionResult(
            path="src/server.c",
            resolved_content="resolved",
            resolution_summary="whitespace-only (no LLM needed)",
            source="automatic",
        )
    ]

    body = BackportPRCreator.build_pr_body(
        context,
        had_conflicts=True,
        resolution_results=results,
    )

    assert "Human Review Required" not in body


def test_create_backport_pr_uses_configured_labels() -> None:
    mock_github = MagicMock()
    mock_repo = MagicMock()
    mock_github.get_repo.return_value = mock_repo

    mock_pr = MagicMock()
    mock_pr.number = 456
    mock_pr.html_url = "https://github.com/owner/repo/pull/456"
    mock_repo.create_pull.return_value = mock_pr

    context = BackportPRContext(
        source_pr_number=123,
        source_pr_title="Fix failover edge case",
        source_pr_url="https://github.com/owner/repo/pull/123",
        source_pr_diff="diff",
        target_branch="8.1",
        commits=["abc1234"],
    )
    result = ResolutionResult(
        path="src/server.c",
        resolved_content="resolved",
        resolution_summary="Applied the null check from the source branch.",
    )

    creator = BackportPRCreator(
        mock_github,
        "owner/repo",
        backport_label="needs-backport-review",
        llm_conflict_label="ai-resolved-conflict",
    )

    pr_url = creator.create_backport_pr(
        context,
        CherryPickResult(success=False, conflicting_files=[], applied_commits=["abc1234"]),
        [result],
        branch_name="backport/123-to-8.1",
    )

    assert pr_url == mock_pr.html_url
    _, create_kwargs = mock_repo.create_pull.call_args
    assert create_kwargs["head"] == "backport/123-to-8.1"
    mock_pr.add_to_labels.assert_called_once_with(
        "needs-backport-review",
        "ai-resolved-conflict",
    )


def test_create_backport_pr_does_not_apply_llm_label_for_automatic_resolution() -> None:
    mock_github = MagicMock()
    mock_repo = MagicMock()
    mock_github.get_repo.return_value = mock_repo

    mock_pr = MagicMock()
    mock_pr.number = 456
    mock_pr.html_url = "https://github.com/owner/repo/pull/456"
    mock_repo.create_pull.return_value = mock_pr

    context = BackportPRContext(
        source_pr_number=123,
        source_pr_title="Whitespace-only conflict",
        source_pr_url="https://github.com/owner/repo/pull/123",
        source_pr_diff="diff",
        target_branch="8.1",
        commits=["abc1234"],
    )
    result = ResolutionResult(
        path="src/server.c",
        resolved_content="resolved",
        resolution_summary="whitespace-only (no LLM needed)",
        source="automatic",
    )

    creator = BackportPRCreator(
        mock_github,
        "owner/repo",
        backport_label="needs-backport-review",
        llm_conflict_label="ai-resolved-conflict",
    )

    creator.create_backport_pr(
        context,
        CherryPickResult(success=False, conflicting_files=[], applied_commits=["abc1234"]),
        [result],
        branch_name="backport/123-to-8.1",
    )

    mock_pr.add_to_labels.assert_called_once_with("needs-backport-review")


def _make_creator_with_repo(
    backport_label: str = "backport",
    llm_conflict_label: str = "ai-resolved-conflicts",
):
    """Build a BackportPRCreator wired to mock github/repo/pr objects."""
    mock_github = MagicMock()
    mock_repo = MagicMock()
    mock_github.get_repo.return_value = mock_repo

    mock_pr = MagicMock()
    mock_pr.number = 999
    mock_pr.html_url = "https://github.com/owner/repo/pull/999"
    mock_repo.create_pull.return_value = mock_pr

    creator = BackportPRCreator(
        mock_github,
        "owner/repo",
        backport_label=backport_label,
        llm_conflict_label=llm_conflict_label,
    )
    return creator, mock_repo, mock_pr


def _basic_context() -> BackportPRContext:
    return BackportPRContext(
        source_pr_number=123,
        source_pr_title="Fix something",
        source_pr_url="https://github.com/owner/repo/pull/123",
        source_pr_diff="diff",
        target_branch="8.1",
        commits=["abc1234"],
    )


def test_ensure_label_exists_creates_missing_backport_label() -> None:
    """When the configured backport label is missing on the repo, the
    creator should create it before applying."""
    from github.GithubException import GithubException

    creator, mock_repo, mock_pr = _make_creator_with_repo()
    mock_repo.get_label.side_effect = GithubException(404, {"message": "Not Found"})

    creator.create_backport_pr(
        _basic_context(),
        CherryPickResult(success=True, conflicting_files=[], applied_commits=["abc1234"]),
        None,
        branch_name="backport/123-to-8.1",
    )

    mock_repo.get_label.assert_called_once_with("backport")
    mock_repo.create_label.assert_called_once()
    create_kwargs = mock_repo.create_label.call_args.kwargs
    assert create_kwargs["name"] == "backport"
    assert create_kwargs["color"] == "0e8a16"
    assert "valkey-ci-agent" in create_kwargs["description"]
    mock_pr.add_to_labels.assert_called_once_with("backport")


def test_ensure_label_exists_creates_both_labels_when_llm_resolved() -> None:
    """Both labels must be created when both are missing and LLM resolved a conflict."""
    from github.GithubException import GithubException

    creator, mock_repo, mock_pr = _make_creator_with_repo()
    mock_repo.get_label.side_effect = GithubException(404, {"message": "Not Found"})

    result = ResolutionResult(
        path="src/server.c",
        resolved_content="resolved",
        resolution_summary="LLM resolved conflict",
    )
    creator.create_backport_pr(
        _basic_context(),
        CherryPickResult(success=False, conflicting_files=[], applied_commits=["abc1234"]),
        [result],
        branch_name="backport/123-to-8.1",
    )

    assert mock_repo.get_label.call_count == 2
    assert mock_repo.create_label.call_count == 2
    created_names = {
        call.kwargs["name"] for call in mock_repo.create_label.call_args_list
    }
    assert created_names == {"backport", "ai-resolved-conflicts"}
    mock_pr.add_to_labels.assert_called_once_with(
        "backport", "ai-resolved-conflicts",
    )


def test_ensure_label_skips_create_when_label_exists() -> None:
    """When get_label succeeds, create_label must not be called."""
    creator, mock_repo, mock_pr = _make_creator_with_repo()
    mock_repo.get_label.return_value = MagicMock()  # label found

    creator.create_backport_pr(
        _basic_context(),
        CherryPickResult(success=True, conflicting_files=[], applied_commits=["abc1234"]),
        None,
        branch_name="backport/123-to-8.1",
    )

    mock_repo.get_label.assert_called_once_with("backport")
    mock_repo.create_label.assert_not_called()
    mock_pr.add_to_labels.assert_called_once_with("backport")


def test_ensure_label_swallows_create_failure_and_still_attempts_apply() -> None:
    """If label creation fails (e.g. permission error), the PR flow continues
    and add_to_labels is still attempted."""
    from github.GithubException import GithubException

    creator, mock_repo, mock_pr = _make_creator_with_repo()
    mock_repo.get_label.side_effect = GithubException(404, {"message": "Not Found"})
    mock_repo.create_label.side_effect = GithubException(
        403, {"message": "Resource not accessible by integration"},
    )

    pr_url = creator.create_backport_pr(
        _basic_context(),
        CherryPickResult(success=True, conflicting_files=[], applied_commits=["abc1234"]),
        None,
        branch_name="backport/123-to-8.1",
    )

    assert pr_url == mock_pr.html_url
    mock_repo.create_label.assert_called_once()
    mock_pr.add_to_labels.assert_called_once_with("backport")


def test_ensure_label_treats_422_as_already_exists() -> None:
    """A 422 from create_label (concurrent creation) should be silently ignored."""
    from github.GithubException import GithubException

    creator, mock_repo, mock_pr = _make_creator_with_repo()
    mock_repo.get_label.side_effect = GithubException(404, {"message": "Not Found"})
    mock_repo.create_label.side_effect = GithubException(
        422, {"message": "Validation Failed"},
    )

    pr_url = creator.create_backport_pr(
        _basic_context(),
        CherryPickResult(success=True, conflicting_files=[], applied_commits=["abc1234"]),
        None,
        branch_name="backport/123-to-8.1",
    )

    assert pr_url == mock_pr.html_url
    mock_pr.add_to_labels.assert_called_once_with("backport")


def test_pull_create_head_ref_uses_plain_branch_for_direct_upstream() -> None:
    branch = "agent/backport/sweep/8.1"

    assert build_pull_create_head_ref("valkey-io/valkey", None, branch) == branch
    assert (
        build_pull_create_head_ref("valkey-io/valkey", "valkey-io/valkey", branch)
        == branch
    )


def test_pull_head_refs_for_different_owner_fork() -> None:
    branch = "agent/backport/sweep/8.1"

    assert (
        build_pull_create_head_ref(
            "valkey-io/valkey",
            "ci-bot/valkey",
            branch,
        )
        == "ci-bot:agent/backport/sweep/8.1"
    )
    assert (
        build_pull_search_head_ref(
            "valkey-io/valkey",
            "ci-bot/valkey",
            branch,
        )
        == "ci-bot:agent/backport/sweep/8.1"
    )


def test_pull_matches_push_repo_filters_unexpected_head_repo() -> None:
    matching = MagicMock()
    matching.head.repo.full_name = "ci-bot/valkey"
    wrong = MagicMock()
    wrong.head.repo.full_name = "valkey-io/valkey"
    unknown = MagicMock()
    unknown.head.repo.full_name = None

    assert pull_matches_push_repo(matching, "ci-bot/valkey")
    assert not pull_matches_push_repo(wrong, "ci-bot/valkey")
    assert not pull_matches_push_repo(unknown, "ci-bot/valkey")




class TestDuplicateDetectionProperty:
    """

    For any source PR number and target branch, the duplicate detection logic
    should identify an existing PR as a duplicate if and only if its head
    branch matches the ``backport/<source-pr-number>-to-<target-branch>``
    pattern.
    """

    @given(
        source_pr_number=st.integers(min_value=1, max_value=999_999),
        target_branch=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N"),
                whitelist_characters=".-_/",
            ),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_detects_duplicate_when_branch_matches(
        self,
        source_pr_number: int,
        target_branch: str,
    ) -> None:
        """check_duplicate returns a PR URL when an open PR has the matching
        head branch."""
        expected_branch = build_branch_name(source_pr_number, target_branch)

        # Mock GitHub client
        mock_github = MagicMock()
        mock_repo = MagicMock()
        mock_github.get_repo.return_value = mock_repo

        # Mock repo.owner.login
        type(mock_repo.owner).login = PropertyMock(return_value="valkey-io")

        # Mock an open PR with matching head branch
        mock_pr = MagicMock()
        mock_pr.html_url = "https://github.com/valkey-io/valkey/pull/999"
        mock_pr.head.repo.full_name = "valkey-io/valkey"

        mock_repo.get_pulls.return_value = [mock_pr]

        creator = BackportPRCreator(mock_github, "valkey-io/valkey")
        result = creator.check_duplicate(source_pr_number, target_branch)

        # Should find the duplicate
        assert result == mock_pr.html_url

        # Verify the search used the correct branch name pattern
        mock_repo.get_pulls.assert_called_once_with(
            state="open",
            head=f"valkey-io:{expected_branch}",
        )

    @given(
        source_pr_number=st.integers(min_value=1, max_value=999_999),
        target_branch=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N"),
                whitelist_characters=".-_/",
            ),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_returns_none_when_no_matching_pr(
        self,
        source_pr_number: int,
        target_branch: str,
    ) -> None:
        """check_duplicate returns None when no PR has the matching head branch."""
        expected_branch = build_branch_name(source_pr_number, target_branch)

        mock_github = MagicMock()
        mock_repo = MagicMock()
        mock_github.get_repo.return_value = mock_repo
        type(mock_repo.owner).login = PropertyMock(return_value="valkey-io")

        # No open or closed PRs match
        mock_repo.get_pulls.return_value = []

        creator = BackportPRCreator(mock_github, "valkey-io/valkey")
        result = creator.check_duplicate(source_pr_number, target_branch)

        assert result is None

        # Verify both open and closed states were searched with correct branch
        calls = mock_repo.get_pulls.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs == {"state": "open", "head": f"valkey-io:{expected_branch}"}
        assert calls[1].kwargs == {"state": "closed", "head": f"valkey-io:{expected_branch}"}

    @given(
        source_pr_number=st.integers(min_value=1, max_value=999_999),
        target_branch=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N"),
                whitelist_characters=".-_/",
            ),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_detects_closed_merged_duplicate(
        self,
        source_pr_number: int,
        target_branch: str,
    ) -> None:
        """check_duplicate finds merged PRs as duplicates (avoid re-merging
        a backport that was already merged and branch recycled).
        A closed-but-unmerged PR should NOT count as a duplicate so the
        agent can reopen a fresh backport if the old one was abandoned.
        """
        expected_branch = build_branch_name(source_pr_number, target_branch)

        mock_github = MagicMock()
        mock_repo = MagicMock()
        mock_github.get_repo.return_value = mock_repo
        type(mock_repo.owner).login = PropertyMock(return_value="valkey-io")

        mock_closed_pr = MagicMock()
        mock_closed_pr.html_url = "https://github.com/valkey-io/valkey/pull/888"
        mock_closed_pr.merged_at = "2026-01-01T00:00:00Z"  # merged → duplicate
        mock_closed_pr.head.repo.full_name = "valkey-io/valkey"

        # No open PRs, but a merged closed one matches
        mock_repo.get_pulls.side_effect = [
            [],  # open search returns nothing
            [mock_closed_pr],  # closed search finds a merged one
        ]

        creator = BackportPRCreator(mock_github, "valkey-io/valkey")
        result = creator.check_duplicate(source_pr_number, target_branch)

        assert result == mock_closed_pr.html_url

        # Verify both searches used the correct branch name
        calls = mock_repo.get_pulls.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs == {"state": "open", "head": f"valkey-io:{expected_branch}"}
        assert calls[1].kwargs == {"state": "closed", "head": f"valkey-io:{expected_branch}"}


    @given(
        source_pr_number=st.integers(min_value=1, max_value=999_999),
        target_branch=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N"),
                whitelist_characters=".-_/",
            ),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=20, deadline=None)
    def test_closed_unmerged_pr_is_not_duplicate(
        self,
        source_pr_number: int,
        target_branch: str,
    ) -> None:
        """A closed-but-not-merged PR should not block re-opening a fresh
        backport — the previous work was abandoned, not shipped."""
        mock_github = MagicMock()
        mock_repo = MagicMock()
        mock_github.get_repo.return_value = mock_repo
        type(mock_repo.owner).login = PropertyMock(return_value="valkey-io")

        mock_closed_pr = MagicMock()
        mock_closed_pr.html_url = "https://github.com/valkey-io/valkey/pull/888"
        mock_closed_pr.merged_at = None  # closed but not merged
        mock_closed_pr.head.repo.full_name = "valkey-io/valkey"

        mock_repo.get_pulls.side_effect = [
            [],  # no open PRs
            [mock_closed_pr],  # closed but not merged
        ]

        creator = BackportPRCreator(mock_github, "valkey-io/valkey")
        result = creator.check_duplicate(source_pr_number, target_branch)

        # Closed-unmerged PR should NOT be treated as a duplicate
        assert result is None
