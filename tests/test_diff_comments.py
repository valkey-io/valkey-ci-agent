from __future__ import annotations

from scripts.backport.diff_comments import (
    marked_source_pr_urls,
    parse_marker,
    reconcile_diff_comments,
    render_diff_comment,
)
from scripts.backport.models import ResolutionResult


class FakeComment:
    _counter = 0

    def __init__(self, body: str, author: str = "valkeyrie-bot[bot]") -> None:
        self.body = body
        self.deleted = False
        self.user = type("User", (), {"login": author})()
        FakeComment._counter += 1
        self.html_url = f"https://github.com/o/r/pull/1#issuecomment-{FakeComment._counter}"

    def edit(self, body: str) -> None:
        self.body = body

    def delete(self) -> None:
        self.deleted = True


class FakePR:
    def __init__(self, comments: list[FakeComment] | None = None) -> None:
        self._comments = comments or []
        self.posted: list[str] = []
        self.html_url = "https://github.com/o/r/pull/1"

    def get_issue_comments(self) -> list[FakeComment]:
        return [c for c in self._comments if not c.deleted]

    def create_issue_comment(self, body: str) -> FakeComment:
        comment = FakeComment(body)
        self._comments.append(comment)
        self.posted.append(body)
        return comment


def _resolved(
    path: str,
    reviewer_diff: str,
    raw_diff: str | None = None,
    *,
    summary: str | None = None,
) -> ResolutionResult:
    return ResolutionResult(
        path=path,
        resolved_content="resolved\n",
        resolution_summary="resolved by Claude Code",
        resolution_diff=raw_diff if raw_diff is not None else reviewer_diff,
        reviewer_diff=reviewer_diff,
        llm_summary=summary,
    )


def _legacy_comment(source_pr: int, path: str, diff: str) -> FakeComment:
    marker = (
        f'<!-- valkey-ci-agent:ai-diff source_pr="{source_pr}" '
        f'path="{path}" sha="0123456789abcdef" -->'
    )
    return FakeComment(f"{marker}\nlegacy body\n{diff}")


def _live(pr: FakePR) -> list[FakeComment]:
    return pr.get_issue_comments()


def test_render_and_parse_grouped_comment() -> None:
    body = render_diff_comment(
        42,
        [_resolved("src/server.c", "-old\n+new", "-<<<<<<< HEAD\n-old\n+new")],
        source_title="Fix RESP3 type",
        cherry_pick_sha="abcdef1234567890",
        repo_html_url="https://github.com/o/r",
        resolved_commit_sha="fedcba9876543210",
        pr_html_url="https://github.com/o/r/pull/1",
    )

    parsed = parse_marker(body)
    assert parsed is not None
    assert parsed.source_pr == 42
    assert parsed.path is None
    assert len(parsed.sha) == 16
    assert "### AI conflict resolution: source PR #42" in body
    assert "**Fix RESP3 type" in body
    assert "`abcdef123456`" in body
    # Link-based: no inlined diff, no raw cleanup, links to the commit view.
    assert "**AI-resolved conflicted files**" in body
    assert "<summary>Resolved hunk</summary>" not in body
    assert "<summary>Raw conflict cleanup</summary>" not in body
    assert "```diff" not in body
    assert "/commit/fedcba9876543210#diff-" in body
    assert "[view diff]" in body
    assert "[commit fedcba987654]" in body


def test_render_includes_single_claude_summary_for_group() -> None:
    body = render_diff_comment(
        7,
        [
            _resolved("a.c", "-a\n+A", summary="Kept target API and source behavior."),
            _resolved("b.c", "-b\n+B", summary="This should not repeat."),
        ],
    )
    assert body.count("**Claude Summary**") == 1
    assert "Kept target API and source behavior." in body


def test_parse_legacy_per_file_marker() -> None:
    parsed = parse_marker(_legacy_comment(7, 'src/&quot;q&quot;.c', "-x\n+y").body)
    assert parsed is not None
    assert parsed.source_pr == 7
    assert parsed.path == 'src/"q".c'
    assert parsed.sha == "0123456789abcdef"


def test_sha_tracks_rendered_payload() -> None:
    # The body links to the resolution commit rather than inlining diffs, so the
    # payload sha tracks the commit sha (what reviewers actually click through to).
    a = parse_marker(render_diff_comment(
        1, [_resolved("f.c", "-a\n+b")],
        repo_html_url="https://github.com/o/r", resolved_commit_sha="a" * 40,
    ))
    b = parse_marker(render_diff_comment(
        1, [_resolved("f.c", "-a\n+b")],
        repo_html_url="https://github.com/o/r", resolved_commit_sha="b" * 40,
    ))
    assert a is not None and b is not None
    assert a.sha != b.sha


def test_reconcile_posts_one_grouped_comment_and_returns_path_urls() -> None:
    pr = FakePR()
    urls = reconcile_diff_comments(
        pr,
        42,
        [_resolved("src/a.c", "-a\n+b"), _resolved("src/b.c", "-c\n+d")],
        source_title="Grouped fix",
    )

    assert len(_live(pr)) == 1
    assert set(urls) == {"src/a.c", "src/b.c"}
    assert set(urls.values()) == {_live(pr)[0].html_url}
    assert "Grouped fix" in _live(pr)[0].body
    assert "src/a.c" in _live(pr)[0].body
    assert "src/b.c" in _live(pr)[0].body


def test_reconcile_many_files_stays_one_comment() -> None:
    pr = FakePR()
    results = [_resolved(f"src/f{i}.c", f"-{i}\n+{i}") for i in range(50)]
    urls = reconcile_diff_comments(pr, 42, results)
    assert len(_live(pr)) == 1
    assert len(urls) == 50


def test_reconcile_leaves_unchanged_comment() -> None:
    result = _resolved("src/a.c", "-a\n+b")
    pr = FakePR()
    existing = FakeComment(render_diff_comment(42, [result], pr_html_url=pr.html_url))
    pr._comments.append(existing)
    reconcile_diff_comments(pr, 42, [result])
    assert pr.posted == []
    assert not existing.deleted
    assert existing.body == render_diff_comment(42, [result], pr_html_url=pr.html_url)


def test_reconcile_edits_changed_comment() -> None:
    # Link-based comments change when the resolution commit changes (a re-run
    # produces a new commit), so reconcile edits in place rather than reposting.
    existing = FakeComment(render_diff_comment(
        42, [_resolved("src/a.c", "-a\n+b")],
        repo_html_url="https://github.com/o/r", resolved_commit_sha="a" * 40,
    ))
    pr = FakePR([existing])
    reconcile_diff_comments(
        pr, 42, [_resolved("src/a.c", "-a\n+b")], resolved_commit_sha="b" * 40,
    )
    assert pr.posted == []
    assert not existing.deleted
    assert ("b" * 40)[:12] in existing.body
    assert "/commit/" + "b" * 40 in existing.body


def test_reconcile_deletes_orphaned_comment() -> None:
    existing = FakeComment(render_diff_comment(42, [_resolved("src/gone.c", "-a\n+b")]))
    pr = FakePR([existing])
    reconcile_diff_comments(pr, 42, [])
    assert existing.deleted
    assert _live(pr) == []


def test_reconcile_ignores_foreign_comments() -> None:
    human = FakeComment("Looks good to me.")
    other_pr = FakeComment(render_diff_comment(999, [_resolved("src/a.c", "-a\n+b")]))
    pr = FakePR([human, other_pr])
    reconcile_diff_comments(pr, 42, [_resolved("src/a.c", "-x\n+y")])
    assert not human.deleted
    assert not other_pr.deleted
    assert len(_live(pr)) == 3


def test_reconcile_migrates_legacy_per_file_markers_to_grouped_comment() -> None:
    legacy_a = _legacy_comment(42, "src/a.c", "-a\n+b")
    legacy_b = _legacy_comment(42, "src/b.c", "-c\n+d")
    pr = FakePR([legacy_a, legacy_b])

    reconcile_diff_comments(
        pr,
        42,
        [_resolved("src/a.c", "-a\n+b"), _resolved("src/b.c", "-c\n+d")],
    )

    live = _live(pr)
    assert len(live) == 1
    # Exactly one legacy comment is promoted into the grouped comment (the
    # keeper); the other is deleted. The survivor is whichever was kept.
    assert legacy_a.deleted != legacy_b.deleted, "exactly one legacy comment should be deleted"
    keeper = legacy_b if legacy_a.deleted else legacy_a
    assert live[0] is keeper, "the surviving comment must be the promoted keeper"
    marker = parse_marker(live[0].body)
    assert marker is not None
    # Promoted to the grouped shape: no per-file path, all files listed.
    assert marker.path is None
    assert marker.source_pr == 42
    assert "src/a.c" in live[0].body
    assert "src/b.c" in live[0].body


def test_reconcile_skips_unresolved_and_diffless() -> None:
    pr = FakePR()
    results = [
        _resolved("src/a.c", "-a\n+b"),
        ResolutionResult(path="src/fail.c", resolved_content=None, resolution_summary="failed"),
        ResolutionResult(
            path="src/nodiff.c",
            resolved_content="x\n",
            resolution_summary="ok",
            resolution_diff=None,
            reviewer_diff=None,
        ),
    ]
    urls = reconcile_diff_comments(pr, 42, results)
    assert set(urls) == {"src/a.c"}
    assert len(_live(pr)) == 1


def test_reconcile_ignores_forged_marker_from_other_author() -> None:
    forged = FakeComment(
        render_diff_comment(42, [_resolved("src/a.c", "-a\n+b")]),
        author="alice",
    )
    pr = FakePR([forged])
    reconcile_diff_comments(pr, 42, [], bot_login="valkeyrie-bot[bot]")
    assert not forged.deleted


def test_reconcile_honors_bot_authored_comment_under_gate() -> None:
    mine = FakeComment(render_diff_comment(42, [_resolved("src/gone.c", "-a\n+b")]))
    pr = FakePR([mine])
    reconcile_diff_comments(pr, 42, [], bot_login="valkeyrie-bot[bot]")
    assert mine.deleted


def test_render_diff_comment_stays_small_regardless_of_diff_size() -> None:
    # Link-based comments never inline diffs, so even a huge resolution cannot
    # push the comment near GitHub's 65,536-char limit.
    big = "\n".join(f"+line {i}" for i in range(200000))
    body = render_diff_comment(
        42,
        [_resolved(f"src/f{i}.c", big, raw_diff=big) for i in range(20)],
        repo_html_url="https://github.com/o/r",
        resolved_commit_sha="c" * 40,
    )
    assert len(body) < 5000
    assert "```diff" not in body
    assert parse_marker(body).source_pr == 42
    # Every file still has a link to its native diff in the commit view.
    assert body.count("[view diff]") == 20


def test_list_marked_source_prs_finds_groups_and_legacy_with_author_gate() -> None:
    from scripts.backport.diff_comments import list_marked_source_prs

    pr = FakePR([
        FakeComment(render_diff_comment(1, [_resolved("a.c", "-x\n+y")])),
        _legacy_comment(2, "b.c", "-x\n+y"),
        FakeComment(render_diff_comment(3, [_resolved("c.c", "-x\n+y")]), author="someone-else"),
        FakeComment("plain human comment"),
    ])
    assert list_marked_source_prs(pr, bot_login="valkeyrie-bot[bot]") == {1, 2}
    assert list_marked_source_prs(pr) == {1, 2, 3}


def test_marked_source_pr_urls_prefers_grouped_comment_and_author_gate() -> None:
    legacy = _legacy_comment(1, "a.c", "-x\n+y")
    grouped = FakeComment(render_diff_comment(1, [_resolved("a.c", "-x\n+y")]))
    foreign = FakeComment(
        render_diff_comment(2, [_resolved("b.c", "-x\n+y")]),
        author="someone-else",
    )
    pr = FakePR([legacy, grouped, foreign, FakeComment("plain human comment")])

    assert marked_source_pr_urls(pr, bot_login="valkeyrie-bot[bot]") == {
        1: grouped.html_url,
    }
    assert marked_source_pr_urls(pr) == {
        1: grouped.html_url,
        2: foreign.html_url,
    }
