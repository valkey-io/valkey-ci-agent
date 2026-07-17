"""Cut a release: generate notes, render a dated section, bump the version.

Each cut generates the notes from the labelled PRs in range and renders them in
one shot. The release-line branch model is tag-driven (one M.m branch per minor):
all stages target the existing M.m branch, and tags determine the discovery range.
The agent never creates or deletes branches.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from scripts.common.git_auth import github_https_url
from scripts.common.proc import BOT_EMAIL, BOT_NAME, git_output, run_git
from scripts.release_notes import contributors as gc
from scripts.release_notes import pipeline as pipeline_mod
from scripts.release_notes import publish as publish_mod
from scripts.release_notes import release_format as rn
from scripts.release_notes import security as security_mod
from scripts.release_notes import version_bump as bv

logger = logging.getLogger(__name__)

NOTES_FILE = "00-RELEASENOTES"
VERSION_FILE = os.path.join("src", "version.h")

# The agent creates only throwaway prep branches in this namespace, which PR
# into the M.m release line. The line is only advanced by merging a PR.
PREP_BRANCH_PREFIX = "agent/release-cut"

_RC_STAGE_RE = re.compile(r"^rc([1-9]\d*)$")
# Matches "Valkey M.m.p-rcN" headings in the release line changelog, to tell
# which rc numbers already shipped.
_DATED_RC_RE_TMPL = r"^Valkey {major}\.{minor}\.{patch}-rc(\d+)"

# A rendered note bullet ends with "(#N)" naming the PR it credits. The
# bullet-line guard keeps a "(#N)" in prose or a heading from being read as a
# credit. Used to dedup a cut's notes against the PRs the destination release
# line already lists (see _drop_already_credited).
_BULLET_LINE_RE = re.compile(r"^\s*[*-]\s+\S")
# Trailing PR ref: "(#N)" at end of line, tolerating trailing punctuation/closing
# parens a hand-edit may add (". ", ": ", ")", "(#44)(#45)"). The agent's own
# render always emits a single canonical "(#N)"; the punctuation tolerance only
# matters for destination-side hand-edits / pre-existing valkey files, where a
# missed ref would let a credited PR be promoted a second time. A trailing run
# like "(#44)(#45)" still captures only the last ref (45), rare enough to leave.
_TRAILING_PR_RE = re.compile(r"\(#(\d+)\)[\s.,:;)]*$")

# Urgency values render_release_notes() accepts; a SECURITY cut with no fixes is
# flagged in the PR body. Mirrors VALID_URGENCIES in the release-format module
# (validated authoritatively there) and the workflow's `urgency` choice list.
_SECURITY_URGENCY = "SECURITY"


@dataclass(frozen=True)
class BranchPlan:
    """How a cut maps onto the release-line branch model."""

    stage: str                 # normalized: 'ga' or 'rcN'
    target: str                # branch to PR into, always M.m (e.g. 9.1)
    base_ref: str              # ref the target is (re)based on
    rc_warning: Optional[str] = None  # set when the requested rc is out of sequence (surfaced in the PR body)


@dataclass(frozen=True)
class _NotesRange:
    """The exact base/head span the notes were computed over, for the PR body."""

    mode: str
    source_ref: str
    target_branch: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str


@dataclass(frozen=True)
class _NotesMeta:
    """Signals about a cut's notes, surfaced in the PR body and dry-run output.

    Bundles everything the body/dry-run renderers need beyond the plan and the
    rendered notes, so adding a new advisory does not grow their signatures.
    """

    regen: Any                          # pipeline.RegenResult for this cut
    already_credited: Sequence[int]     # PRs dropped as already on the line
    noted_bullet_count: int             # bullets actually in the dated section (post already-credited drop)
    urgency: str                        # the requested upgrade urgency
    security_fixes: Optional[Sequence[str]]  # sanitized security bullets: manual + advisory-derived (None when empty)
    security_noted_prs: Sequence[int]   # PRs dropped from generated bullets because supplied as a --security-fix (kept only under Security Fixes)
    baseline_unanchored: bool           # rc1 of M.0.0 with no --base-ref (over-broad range risk)
    advisories: Optional[Any] = None    # security.AdvisorySelection when --security-from-advisories ran, else None
    notes_range: Optional["_NotesRange"] = None  # resolved base/head refs + SHAs for the range display


def _split_version(version: str) -> tuple[int, int, int]:
    # Delegate to release_format.parse_version (the single authoritative M.m.p
    # parser + 0-255 bound, also behind version_num/set_version), so the version
    # validation the cut applies at the input boundary cannot drift from the
    # validation the format primitives apply when writing version.h. Rejecting a
    # malformed/too-large version here fails fast, before the wasted clone + AI run.
    return rn.parse_version(version)


def canonical_version(version: str) -> str:
    """Return the canonical ``M.m.p`` form of *version* (strips, drops leading zeros).

    The single normalization choke point for ``version``, mirroring
    :func:`_normalize_stage` for the stage. Raw dispatch input may carry a trailing
    space (``"9.1.0 "`` -> an invalid prep-branch ref) or leading zeros
    (``"09.1.0"`` -> ``version.h``/heading/commit carry ``09.1.0`` while the branch
    name is ``9.1.0``, a self-inconsistent release). Canonicalizing once and
    threading the result everywhere keeps every downstream value aligned with the
    branch the cut targets. Raises :class:`ValueError` on malformed input.
    """
    major, minor, patch = _split_version(version)
    return f"{major}.{minor}.{patch}"


def _normalize_stage(stage: str) -> str:
    s = stage.strip().lower()
    if s == "ga" or _RC_STAGE_RE.match(s):
        return s
    raise ValueError(f"stage must be 'ga' or 'rcN' (e.g. rc1), got {stage!r}")


def _remote_branch_exists(repo_dir: str, branch: str) -> bool:
    """True if ``refs/heads/<branch>`` exists on ``origin``."""
    out = git_output(repo_dir, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return bool(out.strip())


def _assert_origin_url(repo_dir: str, repo_full_name: str) -> None:
    """Verify the clone's origin URL matches the expected repository.

    Defense-in-depth against a tampered remote (the primary defense is --safe-mode
    on Claude subprocess invocations, which prevents hooks from modifying the clone).
    Skips the check for non-GitHub URLs (local test fixtures).
    """
    try:
        actual = git_output(repo_dir, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        return  # no origin remote (local test fixture)
    if not actual.startswith("https://github.com/"):
        return  # local path or non-GitHub remote (test fixture)
    expected = github_https_url(repo_full_name)
    if actual != expected:
        raise RuntimeError(
            f"Clone origin URL was modified (expected {expected!r}, got {actual!r}). "
            "Aborting to prevent credential disclosure to a tampered remote."
        )


def resolve_branch_plan(repo_dir: str, *, version: str, stage: str) -> BranchPlan:
    """Resolve the destination branch for this cut.

    The target is always M.m (derived from version). The branch must already
    exist on origin (created by maintainers).
    """
    stage_lc = _normalize_stage(stage)
    major, minor, _patch = _split_version(version)
    target = f"{major}.{minor}"

    if not _remote_branch_exists(repo_dir, target):
        raise ValueError(
            f"Release line {target!r} does not exist on origin. Maintainers must "
            f"create the M.m branch before dispatching a release cut."
        )

    rc_warning = None
    if stage_lc != "ga":
        rc_warning = _warn_rc_sequence(repo_dir, target, stage_lc, major, minor, _patch)

    return BranchPlan(stage_lc, target, target, rc_warning=rc_warning)


def _warn_rc_sequence(
    repo_dir: str, target_branch: str, stage_lc: str, major: int, minor: int, patch: int
) -> Optional[str]:
    """Return a warning if a continued rc number is out of sequence; None if OK."""
    m = _RC_STAGE_RE.match(stage_lc)
    if not m:
        return None
    requested = int(m.group(1))
    try:
        run_git(repo_dir, "fetch", "--quiet", "origin", target_branch)
        notes = git_output(repo_dir, "show", f"FETCH_HEAD:{NOTES_FILE}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s to check rc sequence (%s); skipping.", target_branch, exc)
        return None
    pattern = re.compile(_DATED_RC_RE_TMPL.format(major=major, minor=minor, patch=patch), re.MULTILINE)
    seen = sorted({int(x) for x in pattern.findall(notes)})
    highest = max(seen) if seen else 0
    expected = highest + 1
    if requested == expected:
        return None
    if requested <= highest:
        detail = (f"`{stage_lc}` re-cuts an rc the line already records "
                  f"(it lists up to rc{highest}); the next rc should be rc{expected}.")
    else:
        detail = (f"`{stage_lc}` skips ahead: `{target_branch}` records up to rc{highest}, "
                  f"so the next rc should be rc{expected}.")
    logger.warning("Dispatched %s but %s records up to rc%d (expected rc%d). Cutting anyway.",
                   stage_lc, target_branch, highest, expected)
    return detail


def stage_release_name(version: str, stage_lc: str) -> str:
    """``9.1.0`` at ga, else ``9.1.0-rcN``."""
    return version if stage_lc == "ga" else f"{version}-{stage_lc}"


def commit_title(version: str, stage_lc: str) -> str:
    """Match valkey's release commit titles."""
    if stage_lc == "ga":
        return f"Add release notes entry for Valkey {version} GA"
    return f"Update version to {version}-{stage_lc} and add release notes"


def promote_and_bump(
    valkey_clone_dir: str,
    *,
    grouped: dict[str, list[str]],
    dest_notes_text: str,
    dest_version_text: str,
    version: str,
    stage_lc: str,
    urgency: str,
    date: str,
    repo_full_name: str,
    contrib_base: Optional[str],
    contrib_head: str,
    token: Optional[str],
    security_fixes: Optional[Sequence[str]],
) -> tuple[str, str]:
    """Render *grouped* onto the destination changelog and bump the version.

    Returns ``(new_dest_notes, new_version_h)``. ``render_release_notes`` renders
    the categorized bullets into a new dated section atop the destination's running
    changelog, and ``set_version`` rewrites the three version macros. The contributor
    list is generated over ``contrib_base..contrib_head`` and merged into the
    cumulative footer. *valkey_clone_dir* is needed for the git range resolution
    behind the contributor lookup.
    """
    contributors: list[str] = []
    if contrib_base:
        # Resolve both ends to SHAs the GitHub compare API accepts. contrib_base
        # is typically a remote-tracking ref (origin/unstable) and contrib_head a
        # branch ref; both 404 the API and silently fall back to git shortlog
        # (names only, no @handle, bots not filtered). See _compare_ref.
        base_sha = _compare_ref(valkey_clone_dir, contrib_base)
        head_sha = _compare_ref(valkey_clone_dir, contrib_head)
        contributors = gc.list_contributors(
            repo_full_name, base_sha, head_sha, token, repo_dir=valkey_clone_dir
        )
        logger.info(
            "Collected %d contributor(s) over %s..%s",
            len(contributors), contrib_base, contrib_head,
        )
    else:
        logger.warning("No contributor base ref/tag found; skipping contributor list")

    new_notes = rn.render_release_notes(
        grouped,
        version=version,
        stage=stage_lc,
        urgency=urgency,
        date=date,
        prior_text=dest_notes_text,
        contributors=contributors,
        security_fixes=list(security_fixes) if security_fixes else None,
    )
    new_version = bv.set_version(dest_version_text, version, stage_lc)
    logger.info(
        "version.h -> VALKEY_VERSION=%s VALKEY_VERSION_NUM=%s VALKEY_RELEASE_STAGE=%s",
        version, bv.version_num(version), stage_lc,
    )
    return new_notes, new_version


def _contrib_base(
    repo_dir: str, *, explicit: Optional[str], notes_base_ref: Optional[str]
) -> Optional[str]:
    """Pick the contributor-range start.

    Order: explicit ``--contrib-base-ref``, then the notes baseline, then last
    tag, then root commit. When the notes baseline was pinned (``notes_base_ref``),
    it is used before ``git describe`` so the credits span the same range as the
    bullets. The describe/root fallbacks keep the contributor list from being empty
    when no pinned baseline is available.
    """
    if explicit:
        return explicit
    if notes_base_ref:
        return notes_base_ref
    try:
        tag = git_output(repo_dir, "describe", "--tags", "--abbrev=0").strip()
        if tag:
            return tag
    except Exception:  # noqa: BLE001 - no tag reachable; fall through to root
        pass
    return _root_commit(repo_dir)


def _root_commit(repo_dir: str, ref: str = "HEAD") -> Optional[str]:
    """The oldest root commit reachable from *ref*, or None if it cannot be read.

    Used as the last-resort range base when no tagged baseline exists: a
    ``<root>..head`` walk is the fullest range the head can produce. A history
    with several roots (a repo built by merging unrelated trees) picks the last
    one ``rev-list`` prints (the oldest), so the range stays as complete as
    possible.
    """
    try:
        roots = [r for r in git_output(
            repo_dir, "rev-list", "--max-parents=0", ref
        ).split("\n") if r.strip()]
    except Exception:  # noqa: BLE001 - unreadable history: caller degrades further
        return None
    return roots[-1].strip() if roots else None


def _compare_ref(repo_dir: str, ref: str) -> str:
    """Resolve *ref* to a commit SHA the GitHub compare API can use.

    ``contributors.list_contributors`` hits ``GET /compare/{base}...{head}``,
    which only accepts refs the server knows: a branch/tag name or a full commit
    SHA. The contributor base and head we have locally are neither. The base is a
    remote-tracking ref (``origin/unstable``, because the clone is
    ``--branch <source>`` so other branches exist only as ``origin/<name>``) and
    the head is the literal ``HEAD``. Both resolve fine for git but 404 the
    compare API, which silently drops to the ``git shortlog`` fallback:
    names-only, no ``@handle``, no ``[bot]`` filtering. Dereferencing each to its
    SHA here keeps the API path, and thus the ``Full Name @handle`` format,
    working. Falls back to the ref as given if it cannot be resolved (e.g. no
    local clone), so the contributor step degrades rather than crashing.
    """
    try:
        return git_output(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip() or ref
    except subprocess.CalledProcessError:
        return ref



def _plan_mode(plan: BranchPlan) -> str:
    """A short human label for the resolved branch plan."""
    return plan.stage


def _resolve_notes_range(
    repo_dir: str, plan: BranchPlan, *, head_ref: str, regen: Any
) -> _NotesRange:
    """Capture the exact base/head refs and SHAs discovery walked, for the body."""
    base_ref = regen.base_tag
    return _NotesRange(
        mode=_plan_mode(plan),
        source_ref=plan.target,
        target_branch=plan.target,
        base_ref=base_ref,
        base_sha=_compare_ref(repo_dir, base_ref),
        head_ref=head_ref,
        head_sha=_compare_ref(repo_dir, head_ref),
    )


def _credited_pr_numbers(notes_text: str) -> set[int]:
    """Return the PR numbers a release-line changelog already credits.

    Reads every bullet line's trailing ``(#N)`` from *notes_text* (a destination
    changelog: the dated sections on the M.m release line). This is the dedup
    key for promotion. Upstream, discovery excludes prior-RC PRs via the RC tag
    it walks back to, but the agent never pushes those tags and a fork carries
    none, so on GA (or any continued cut) discovery re-walks the whole source
    branch and re-finds PRs the line already shipped. Deduping the cut's bullets
    against this set makes promotion idempotent regardless of tags: a PR the line
    already lists is dropped instead of double-noted.

    Bullets inside the ``### Security Fixes`` section are skipped: that section is
    sourced only from ``security_fixes`` (never from PR bullets), so its bullets
    carry no legitimate PR credit. Their trailing ``(#N)``, if a CVE summary
    happens to end in one, is prose, not a credit, and must not seed the dedup
    set, or a later cut would drop an unrelated real PR that reused that number.
    """
    security_header = getattr(rn, "SECURITY_CATEGORY", "Security Fixes")
    credited: set[int] = set()
    in_security = False
    for line in notes_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            in_security = stripped[len("### "):].strip() == security_header
            continue
        # Defensive: a "## " ATX header would leave whatever ### category we were
        # in. render_release_notes emits dated sections setext-style (heading +
        # "-" underline), not as "## " headers, so this does not fire on our own
        # output; it guards a hand-edited or differently-formatted changelog.
        if stripped.startswith("## "):
            in_security = False
            continue
        if in_security or not _BULLET_LINE_RE.match(line):
            continue
        m = _TRAILING_PR_RE.search(line)
        if m:
            credited.add(int(m.group(1)))
    return credited


def _grouped_pr_numbers(grouped: dict[str, list[str]]) -> set[int]:
    """Return the PR numbers credited by the bullets in *grouped*.

    Each rendered bullet ends with the canonical trailing ``(#N)``; this reads
    those. Used to intersect this cut's PRs with what the destination already
    credits (dedup) and with the ``--security-fix`` refs (to drop a PR from the
    generated bullets when it is also supplied as a security fix).
    """
    numbers: set[int] = set()
    for lines in grouped.values():
        for line in lines:
            m = _TRAILING_PR_RE.search(line)
            if m:
                numbers.add(int(m.group(1)))
    return numbers


def _drop_already_credited(
    grouped: dict[str, list[str]], credited: set[int]
) -> tuple[dict[str, list[str]], list[int]]:
    """Drop bullets whose trailing ``(#N)`` is in *credited* from *grouped*.

    Returns ``(filtered_grouped, dropped_numbers)``. A category left with no
    bullets is dropped entirely; render_release_notes already omits empty
    categories, so this just keeps the map tidy.
    """
    if not credited:
        return grouped, []
    kept: dict[str, list[str]] = {}
    dropped: list[int] = []
    for category, lines in grouped.items():
        kept_lines: list[str] = []
        for line in lines:
            m = _TRAILING_PR_RE.search(line)
            if m and int(m.group(1)) in credited:
                dropped.append(int(m.group(1)))
                continue
            kept_lines.append(line)
        if kept_lines:
            kept[category] = kept_lines
    return kept, dropped


def _sanitize_security_fixes(
    security_fixes: Optional[Sequence[str]],
) -> Optional[Sequence[str]]:
    """Collapse each ``--security-fix`` entry to one line and drop empty ones.

    Returns ``None`` when nothing usable remains (so the Security Fixes header is
    omitted entirely). ``--security-fix`` bullets bypass the render sanitization AI
    bullets get: valkey's ``emit_category`` only strips and prepends ``* ``, so an
    embedded newline would inject a raw non-bullet line (or a stray ``##`` heading)
    into the changelog. Collapsing on the same boundaries ``str.splitlines`` uses
    keeps "one line" consistent with the format parser.
    """
    if not security_fixes:
        return None
    cleaned = [" ".join(entry.splitlines()).strip() for entry in security_fixes]
    cleaned = [entry for entry in cleaned if entry]
    return cleaned or None


def _security_fix_prs_in_notes(
    security_fixes: Optional[Sequence[str]], noted: set[int]
) -> list[int]:
    """Return PR numbers credited both as a ``--security-fix`` and a normal bullet.

    Reads each security entry's trailing ``(#N)`` (the same canonical reference the
    notes use) and intersects with *noted* (the PRs this cut renders as normal
    bullets). A match means the change would be listed twice: once under **Security
    Fixes** and once under its generated category. The caller drops the normal
    bullet so the change appears only under Security Fixes, where it is reviewed as
    a factual, hand-authored entry (see :func:`cut`). Sorted for a deterministic
    dropped-list in the log and PR body.
    """
    if not security_fixes:
        return []
    found: set[int] = set()
    for entry in security_fixes:
        m = _TRAILING_PR_RE.search(entry)
        if m and int(m.group(1)) in noted:
            found.add(int(m.group(1)))
    return sorted(found)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def cut(
    repo: Any,
    *,
    repo_full_name: str,
    source_clone_dir: str,
    valkey_clone_dir: str,
    version: str,
    stage: str,
    urgency: str,
    date: str,
    tag_glob: Optional[str],
    base_ref: Optional[str],
    contrib_base_ref: Optional[str],
    security_fixes: Optional[Sequence[str]],
    token: str,
    git_env: dict[str, str],
    dry_run: bool,
    baseline_unanchored: bool = False,
    security_from_advisories: bool = False,
    force_ready: bool = False,
) -> int:
    """Cut a release: generate notes with AI, render onto the release line, open PRs.

    ``source_clone_dir`` is a clone of the M.m branch; it doubles as
    ``valkey_clone_dir`` for the contributor range lookup. The destination
    release branch is materialized in a worktree under it. Returns 0 on success,
    1 on failure.

    When *security_from_advisories* is set, published GitHub repository advisories
    fixed by *version* are rendered into the Security Fixes section (merged with
    any manual ``--security-fix`` entries, which win on CVE collision). See
    :mod:`scripts.release_notes.security` for why this is a version-string match,
    not the PR graph-walk, and why embargoed advisories are surfaced as a
    reviewer disclaimer rather than auto-included.

    By default a cut that raises any reviewer-facing signal (see
    :func:`_hold_reasons`) opens its release PR as a draft, which GitHub
    refuses to merge, holding the release line until a maintainer resolves what was
    flagged and marks it ready. Set *force_ready* to open the PR ready for review
    regardless (the banner records that the flags were overridden). A clean cut
    always opens ready.
    """
    # Canonicalize once at the boundary so version.h, the dated heading, the commit
    # title, the prep-branch ref, and the release line all carry the same string.
    # Raw input may have a trailing space or leading zeros (see canonical_version).
    version = canonical_version(version)
    # Auto-derive Security Fixes from published advisories fixed by this version,
    # merged with manual --security-fix entries (manual wins on CVE collision).
    # Fetch never raises: a permission gap degrades to an empty selection whose
    # disclaimer asks a maintainer to add fixes by hand.
    advisories = None
    if security_from_advisories:
        advisories = security_mod.collect_advisory_fixes(repo, version)
        security_fixes = security_mod.merge_with_manual(advisories.matched, security_fixes)
    # Drop empty/whitespace security entries and collapse each to one physical
    # line: unlike AI bullets (sanitized in render._one_line), these bypass render
    # and an embedded newline would inject a raw non-bullet line into the changelog.
    security_fixes = _sanitize_security_fixes(security_fixes)
    plan = resolve_branch_plan(
        source_clone_dir, version=version, stage=stage
    )
    source_ref = plan.target
    logger.info(
        "Plan: stage=%s target=%s base=%s", plan.stage, plan.target, plan.base_ref,
    )

    # Discovery range: head is always the M.m branch tip, base from tags.
    # Pin the head SHA so discovery, contributors, and worktree all refer to the
    # same commit. Without pinning, a PR merged during AI generation would enter
    # the worktree base but be absent from the notes and contributor list.
    notes_base_ref, notes_tag_glob = base_ref, tag_glob
    pinned_head_sha = git_output(source_clone_dir, "rev-parse", source_ref).strip()
    notes_head_ref = pinned_head_sha
    logger.info("Pinned head for discovery: %s -> %s", source_ref, pinned_head_sha[:12])

    if baseline_unanchored and notes_base_ref is None:
        root = _root_commit(source_clone_dir, notes_head_ref)
        if root is not None:
            notes_base_ref = root
            logger.warning("No tagged baseline for %s; root..%s range may be over-broad.", version, notes_head_ref)

    # 1. Generate the categorized bullets: labelled PRs plus the label-less ones
    #    AI triage judged user-facing (see pipeline.regenerate_unreleased).
    regen = pipeline_mod.regenerate_unreleased(
        repo, source_clone_dir, head_ref=notes_head_ref,
        tag_glob=notes_tag_glob, base_ref=notes_base_ref,
    )
    if regen.included and not regen.bullet_count:
        logger.error(
            "%d PR(s) included but no bullets generated; refusing to cut empty notes.",
            regen.included,
        )
        return 1
    grouped = dict(regen.grouped)  # {category: [bullet line, ...]} for this cut

    # 2. Materialize a throwaway worktree at the release line's base. We never
    #    check out (or force-push) the real release branch; instead we build the
    #    promoted commit on an agent-namespaced prep branch and PR it into the
    #    release line, so the line only advances when a human merges. The prep
    #    branch starts from origin/<base_ref> so the PR diff is exactly the cut.
    #    Defense-in-depth: verify origin URL hasn't been tampered with before any
    #    authenticated fetch/push (the primary defense is --safe-mode on Claude,
    #    which prevents hooks from modifying the clone).
    _assert_origin_url(source_clone_dir, repo_full_name)
    run_git(source_clone_dir, "fetch", "origin", plan.base_ref, env=git_env)
    prep_branch = f"{PREP_BRANCH_PREFIX}/{version}-{plan.stage}"
    dest_dir = os.path.join(source_clone_dir, ".release-dest")
    run_git(source_clone_dir, "worktree", "add", "--force", "-B", prep_branch, dest_dir,
            f"origin/{plan.base_ref}")
    try:
        dest_notes_path = os.path.join(dest_dir, NOTES_FILE)
        dest_notes_text = _read(dest_notes_path)
        dest_version_text = _read(os.path.join(dest_dir, VERSION_FILE))

        # Drop bullets the destination changelog already credits. The tag-based
        # dedup in discovery cannot engage without RC tags (the agent never
        # pushes them; a fork has none), so a continued cut (most visibly GA
        # after the final RC) otherwise re-notes every PR the line already
        # shipped. With nothing new, the dated section renders empty (heading +
        # version bump only) and the PR body says so. This is a no-op upstream,
        # where discovery already returns only new PRs.
        already_credited = sorted(
            _credited_pr_numbers(dest_notes_text)
            & _grouped_pr_numbers(grouped)
        )
        if already_credited:
            grouped, _dropped = _drop_already_credited(grouped, set(already_credited))
            logger.info(
                "Dropped %d PR(s) already credited on %s: %s",
                len(already_credited), plan.target, already_credited,
            )

        # Anchor contributors to the same baseline the bullets used (regen.base_tag
        # is the resolved tag for rc2+/ga, or the pinned base_ref / rc1 default),
        # so the credits never span a different range than the notes.
        contrib_base = _contrib_base(
            source_clone_dir, explicit=contrib_base_ref,
            notes_base_ref=regen.base_tag,
        )

        # A --security-fix bullet whose trailing (#N) also names a release-noted PR
        # in this cut would list the same change twice: under Security Fixes and
        # under its generated category. Security Fixes are hand-authored, factual,
        # and reviewed separately, so the security entry is authoritative; drop the
        # generated bullet for that PR so the change appears only under Security
        # Fixes. Match against the PRs actually noted now (grouped post
        # already-credited drop), then re-drop from grouped before rendering and the
        # noted_bullet_count below, so accounting reflects the deduped section.
        security_noted_prs = _security_fix_prs_in_notes(
            security_fixes, _grouped_pr_numbers(grouped)
        )
        if security_noted_prs:
            grouped, _dropped = _drop_already_credited(grouped, set(security_noted_prs))
            logger.info(
                "Dropped %d generated bullet(s) also supplied as a --security-fix "
                "(kept only under Security Fixes): %s",
                len(security_noted_prs), security_noted_prs,
            )

        # 3. Render bullets -> dated section on dest; bump version.h.
        new_dest_notes, new_version = promote_and_bump(
            valkey_clone_dir,
            grouped=grouped,
            dest_notes_text=dest_notes_text,
            dest_version_text=dest_version_text,
            version=version, stage_lc=plan.stage, urgency=urgency, date=date,
            repo_full_name=repo_full_name, contrib_base=contrib_base,
            contrib_head=notes_head_ref, token=token,
            security_fixes=security_fixes,
        )

        # Count what survives into the dated section after the already-credited
        # drop. When some PRs were dropped as duplicates but others remain, this
        # is > 0 and the cut still ships real notes; only when it is 0 is the cut
        # version-bump-only. The "No new release notes" section keys on this so a
        # cut that drops a duplicate yet adds a new note is not mislabelled empty.
        noted_bullet_count = sum(len(lines) for lines in grouped.values())

        # Resolve the exact base/head refs + SHAs the notes were computed over, so
        # the PR body/dry-run can show an auditable range (not just "base..HEAD").
        notes_range = _resolve_notes_range(
            source_clone_dir, plan, head_ref=notes_head_ref, regen=regen,
        )

        notes_meta = _NotesMeta(
            regen=regen, already_credited=already_credited,
            noted_bullet_count=noted_bullet_count, urgency=urgency,
            security_fixes=security_fixes, security_noted_prs=security_noted_prs,
            baseline_unanchored=baseline_unanchored, advisories=advisories,
            notes_range=notes_range,
        )

        if dry_run:
            _print_dry_run(plan, version, new_dest_notes, new_version, notes_meta,
                           force_ready=force_ready)
            return 0

        _write(dest_notes_path, new_dest_notes)
        _write(os.path.join(dest_dir, VERSION_FILE), new_version)

        # Freshness check: verify the target branch hasn't advanced since we
        # pinned it for discovery. A new merge during AI generation would be in
        # the worktree base but absent from the notes and contributor list.
        run_git(source_clone_dir, "fetch", "--quiet", "origin", plan.target, env=git_env)
        current_head = git_output(source_clone_dir, "rev-parse", f"origin/{plan.target}").strip()
        if current_head != pinned_head_sha:
            raise RuntimeError(
                f"Target branch {plan.target!r} advanced during generation "
                f"(pinned {pinned_head_sha[:12]}, now {current_head[:12]}). "
                "Re-run the cut to include the new commits."
            )

        release_url = _commit_push_release_pr(
            repo, dest_dir, repo_full_name=repo_full_name, plan=plan,
            version=version, prep_branch=prep_branch, notes_meta=notes_meta,
            git_env=git_env, force_ready=force_ready,
        )
        logger.info("Release PR: %s", release_url)

        return 0
    finally:
        run_git(source_clone_dir, "worktree", "remove", "--force", dest_dir)


def _print_dry_run(
    plan, version, dest_notes, version_h, notes_meta: "_NotesMeta", *, force_ready: bool = False
) -> None:
    regen = notes_meta.regen
    print(f"\n===== release plan ({version} {plan.stage}) =====")
    print(f"target branch: {plan.target}  base: {plan.base_ref}")
    # Preview the hold decision the real cut would make: a draft PR (held) when any
    # reviewer-facing signal fired and force_ready was not set, else opened ready.
    hold_reasons = _hold_reasons(plan, notes_meta)
    if hold_reasons and not force_ready:
        print(f"PR would open: DRAFT (held) - {len(hold_reasons)} item(s): {'; '.join(hold_reasons)}")
    elif hold_reasons:
        print(f"PR would open: ready (force_ready overrides {len(hold_reasons)} flagged item(s))")
    else:
        print("PR would open: ready (clean cut)")
    # The resolved discovery range is the actual span the notes were computed over;
    # plan.base_ref is the branch-model base, which can differ (e.g. nearest-tag
    # fallback). Print the precise base/head refs + SHAs so an over-broad range
    # shows, falling back to the coarse one-liner only if it could not be captured.
    if notes_meta.notes_range is not None:
        print("notes range:")
        for line in _notes_range_lines(notes_meta.notes_range):
            print(f"  {line}")
    else:
        print(f"notes range: {regen.base_tag}..HEAD")
    if notes_meta.baseline_unanchored:
        print(f"⚠️  baseline unanchored: rc1 of {version} fell back to nearest tag {regen.base_tag!r}")
    if plan.rc_warning:
        print(f"⚠️  rc out of sequence: {plan.rc_warning}")
    if notes_meta.already_credited:
        print(f"already credited on {plan.target} (dropped): {list(notes_meta.already_credited)}")
    if regen.duplicate_prs:
        print(f"⚠️  PR(s) noted more than once (extra bullets dropped): {list(regen.duplicate_prs)}")
    if regen.skipped:
        print(f"⚠️  model declined (labelled but no bullet): {list(regen.skipped)}")
    if regen.uncertain:
        flagged = [f"#{n.pr_number} ({n.reason or 'no reason'})" for n in regen.uncertain]
        print(f"⚠️  notes to double-check: {flagged}")
    if not regen.had_prs:
        print("note: no PRs in range (empty dated section)")
    if notes_meta.advisories is not None:
        sel = notes_meta.advisories
        if sel.fetch_failed:
            print(f"⚠️  advisory fetch failed ({sel.fetch_error}); no CVEs auto-added")
        else:
            matched = [f.display_id for f in sel.matched]
            print(f"advisories: {sel.considered} published, matched {matched or 'none'}")
            if sel.unmatched_ids:
                print(f"advisories not matching {version}: {list(sel.unmatched_ids)}")
            if sel.unreadable_ids:
                print(f"⚠️  advisories unreadable (may match {version}): {list(sel.unreadable_ids)}")
    if notes_meta.security_noted_prs:
        print(f"security fix supplied for noted PR(s); dropped from generated bullets "
              f"(kept only under Security Fixes): {list(notes_meta.security_noted_prs)}")
    if notes_meta.urgency.strip().upper() == _SECURITY_URGENCY and not notes_meta.security_fixes:
        print("⚠️  urgency SECURITY but no security-fix entries")
    if regen.ai_included:
        print("AI-triaged into notes (unlabelled but judged user-facing): "
              f"{[p.number for p in regen.ai_included]}")
    if regen.ai_excluded:
        print("AI-triaged out (unlabelled, judged internal-only): "
              f"{[p.number for p in regen.ai_excluded]}")
    if regen.triage:
        print(f"triage PRs (AI undecided): {[p.number for p in regen.triage]}")
    if regen.unresolved:
        print(f"⚠️  commits with no resolvable PR: {[c.sha[:12] for c in regen.unresolved]}")
    if regen.unresolved_prs:
        print("⚠️  commits whose PR could not be fetched: "
              f"{[(u.sha[:12], u.number) for u in regen.unresolved_prs]}")
    if regen.unresolved_backports:
        print("⚠️  notes credited to a backport (original PR not recovered): "
              f"{[bp.number for bp in regen.unresolved_backports]}")
    if regen.unresolved_cherry_picks:
        print("⚠️  notes with an unconfirmed cherry-pick origin: "
              f"{[cp.number for cp in regen.unresolved_cherry_picks]}")
    if regen.collided:
        print("⚠️  distinct commits dropped by a reused PR number: "
              f"{[(c.sha[:12], c.number) for c in regen.collided]}")
    print(f"\n===== {NOTES_FILE} (release branch, dry run) =====\n{dest_notes}")
    print(f"\n===== {VERSION_FILE} (dry run) =====\n{version_h}")


def _commit_push_release_pr(
    repo: Any, dest_dir: str, *, repo_full_name: str, plan: BranchPlan, version: str,
    prep_branch: str, notes_meta: "_NotesMeta", git_env: dict[str, str],
    force_ready: bool = False,
) -> str:
    """Commit the cut on the prep branch, push it, and open/update a PR into the line.

    The PR is ``head=prep_branch`` into ``base=plan.target`` (the release line),
    so it shows exactly the promoted diff and merges into the line, never the
    self-referential merge-back-into-source shape the release line must avoid.
    The prep branch is agent-namespaced, so force-with-lease on it is safe.
    *notes_meta* carries the advisories surfaced in the body (out-of-sequence rc,
    branch-model anomalies, unanchored baseline, empty/duplicate notes, security
    correlations, AI-triage include/exclude decisions, undecided PRs).

    When those signals name anything a maintainer should address first (see
    :func:`_hold_reasons`), the PR is opened as a draft to hold the merge,
    unless *force_ready* overrides that. The same reasons lead the body as a
    banner. On re-dispatch, the draft state is reconciled to this cut's decision,
    so clearing the flagged items and re-cutting flips a held PR ready on its own.
    """
    run_git(dest_dir, "config", "user.name", BOT_NAME)
    run_git(dest_dir, "config", "user.email", BOT_EMAIL)
    run_git(dest_dir, "add", NOTES_FILE, VERSION_FILE)
    run_git(dest_dir, "commit", "-s", "-m", commit_title(version, plan.stage))
    if not prep_branch.startswith(f"{PREP_BRANCH_PREFIX}/"):
        raise RuntimeError(f"Refusing to push to non-namespaced prep branch: {prep_branch!r}")

    # If an existing PR is open for this prep branch, convert it to draft before
    # force-pushing. This prevents a window where the PR has new content but
    # retains its old body and ready-for-merge state (which could auto-merge if
    # branch protection allows it). The PR is marked ready only after BOTH the
    # branch push and the body update succeed.
    hold = bool(_hold_reasons(plan, notes_meta)) and not force_ready
    title = commit_title(version, plan.stage)
    body = _build_pr_body(plan, version, notes_meta, force_ready=force_ready)
    existing = publish_mod.find_existing_pr(
        repo, base_repo=repo_full_name, push_repo=None, branch=prep_branch,
        base_branch=plan.target,
    )
    if existing is not None and not existing.draft:
        publish_mod.reconcile_draft(existing, draft=True)
        logger.info("Converted PR #%s to draft before branch update", existing.number)

    # Give --force-with-lease a valid basis. The fresh `git clone --branch <M.m>`
    # never fetched this agent-namespaced prep branch, so its remote-tracking ref is
    # absent and the implicit lease expects "branch absent". A prep branch left by an
    # earlier cut of the same stage is present on the remote, so that mismatch rejects
    # the push with "stale info". Fetch it (explicit refspec updates the tracking ref,
    # not just FETCH_HEAD) so the lease matches the real remote tip and the overwrite
    # is accepted; on a first cut the branch is absent and the push creates it.
    if _remote_branch_exists(dest_dir, prep_branch):
        run_git(dest_dir, "fetch", "origin",
                f"+refs/heads/{prep_branch}:refs/remotes/origin/{prep_branch}", env=git_env)
    run_git(dest_dir, "push", "--force-with-lease", "origin", f"HEAD:{prep_branch}", env=git_env)

    # Now update body and reconcile draft state. If this is a re-cut of an
    # existing PR, both the content and the metadata are consistent only after
    # this point.
    return publish_mod.open_or_update_pr(
        repo, base_repo=repo_full_name, push_repo=None, branch=prep_branch,
        base_branch=plan.target, title=title, body=body, existing=existing,
        draft=hold,
    )


def _hold_reasons(plan: BranchPlan, notes_meta: "_NotesMeta") -> list[str]:
    """Every signal in this cut that a maintainer should address before merging.

    One short label per body section that would render. The list is the hold
    decision: a non-empty list means the release PR opens as a draft (blocking the
    merge) unless the cut was dispatched with ``force_ready`` (see
    :func:`_commit_push_release_pr`), and it seeds the "held" banner at the top of
    the body. Each condition mirrors exactly the guard of the section helper it
    names, so the banner can never claim a hold the body does not also explain, nor
    stay silent while a warning section renders below it.

    A clean advisory match (``--security-from-advisories`` with every advisory read
    and matched) is informational, not a warning, so it does not hold; only the
    advisory sub-cases that render a ``⚠️`` (fetch failed, or an advisory that could
    not be read) do. Deduping a PR that leaves real notes behind also does not
    hold: it renders no body section (only the version-bump-only case does, via
    :func:`_no_new_prs_section`).
    """
    regen = notes_meta.regen
    reasons: list[str] = []
    if plan.rc_warning:
        reasons.append("release candidate out of sequence")
    if notes_meta.baseline_unanchored:
        reasons.append("release-notes baseline is unanchored")
    # Empty dated section, and not the dedup cause (which has its own reason next).
    # Mirrors _empty_notes_section's two sub-causes: no PRs, or PRs existed but none
    # were included (every candidate AI-excluded or left undecided). A non-empty
    # security_fixes list counts as content (a security-only cut is legitimate, not
    # a generation miss).
    if not (regen.bullet_count or notes_meta.already_credited or notes_meta.security_fixes) and (
        not regen.had_prs or not regen.included
    ):
        reasons.append("empty release notes")
    if notes_meta.already_credited and not (notes_meta.noted_bullet_count or notes_meta.security_fixes):
        reasons.append("no new release notes (every PR already credited)")
    if regen.duplicate_prs:
        reasons.append("a PR was noted more than once")
    if regen.skipped:
        reasons.append("model declined to note some labelled PRs")
    if regen.uncertain:
        reasons.append("notes flagged low-confidence")
    # AI decided inclusion for label-less PRs (a call that used to require a human
    # label), so a maintainer confirms the include/exclude table before shipping.
    if regen.ai_included or regen.ai_excluded:
        reasons.append("AI triaged unlabelled PRs (confirm include/exclude)")
    sel = notes_meta.advisories
    if sel is not None and sel.fetch_failed:
        reasons.append("security advisories could not be read")
    elif sel is not None and getattr(sel, "unreadable_ids", None):
        reasons.append("some security advisories could not be read")
    # A PR supplied as a --security-fix is now dropped from the generated bullets
    # automatically (see _security_dedup_section), so it is a resolved, informational
    # case, not a hold reason. Only the urgency-with-no-fixes mismatch below holds.
    if notes_meta.urgency.strip().upper() == _SECURITY_URGENCY and not notes_meta.security_fixes:
        reasons.append("SECURITY urgency with no security-fix entries")
    if regen.triage:
        reasons.append("AI triage could not decide some PRs")
    if regen.unresolved:
        reasons.append("commits with no resolvable PR")
    if regen.unresolved_prs:
        reasons.append("commits whose PR could not be fetched")
    if regen.unresolved_backports:
        reasons.append("notes credited to a backport")
    if regen.unresolved_cherry_picks:
        reasons.append("notes with an unconfirmed cherry-pick origin")
    if regen.collided:
        reasons.append("a distinct commit was dropped by a reused PR number")
    return reasons


def _hold_banner(reasons: Sequence[str], force_ready: bool) -> str:
    """Render the top-of-body banner reflecting the hold decision.

    Returns "" on a clean cut (no reasons). When reasons exist, the banner tells a
    reviewer either that the PR was held as a draft (the default) or that it was
    opened ready despite the flags because the cut was dispatched with
    ``force_ready``. Either way it lists the flagged items so the decision is
    visible without scrolling the sections below.
    """
    if not reasons:
        return ""
    items = "; ".join(reasons)
    n = len(reasons)
    plural = "item" if n == 1 else "items"
    if force_ready:
        return (
            "> [!NOTE]\n"
            f"> **Opened ready despite {n} flagged {plural}** (`force_ready` was set): "
            f"{items}. Review the sections below and confirm before merging.\n\n"
        )
    return (
        "> [!WARNING]\n"
        f"> **Held as a draft: do not merge until reviewed.** This cut raised {n} "
        f"{plural} for a maintainer to address first: {items}. Resolve them (see the "
        "sections below), then click **Ready for review** to release, or re-dispatch "
        "with `force_ready` to open ready without changes.\n\n"
    )


def _build_pr_body(
    plan: BranchPlan, version: str, notes_meta: "_NotesMeta", *, force_ready: bool = False
) -> str:
    """Assemble the release PR body: hold banner, summary line, then each section.

    Sections are appended in a fixed, reviewer-friendly order: the most actionable
    "is this the right cut?" warnings (sequence, branch model, baseline) first,
    then the "why do the notes look like this?" explanations (empty, duplicate,
    security), then the AI-triage tables (included / excluded / undecided). Each
    section helper returns "" when it does not apply, so the body stays quiet on a
    clean cut. When the cut raised any hold
    reason (see :func:`_hold_reasons`), a banner leads the body reflecting whether
    the PR was held as a draft or opened ready via *force_ready*. A purely
    informational section (a clean advisory match) renders without a banner.
    """
    regen = notes_meta.regen
    return (
        _hold_banner(_hold_reasons(plan, notes_meta), force_ready)
        + f"Cuts **{stage_release_name(version, plan.stage)}** onto release line "
        f"`{plan.target}`.\n\n"
        f"- Promotes the release notes into a dated section, bumps "
        f"`src/version.h`, and refreshes the running contributor list.\n"
        + _notes_range_body_section(notes_meta.notes_range, regen)
        + _rc_warning_section(plan)
        + _baseline_warning_section(notes_meta, version)
        + _empty_notes_section(notes_meta, plan)
        + _no_new_prs_section(notes_meta, plan)
        + _duplicate_pr_section(regen.duplicate_prs)
        + _skipped_section(regen.skipped)
        + _uncertain_section(regen.uncertain)
        + _advisory_section(notes_meta)
        + _security_dedup_section(notes_meta)
        + _security_warning_section(notes_meta)
        + _ai_included_section(regen.ai_included)
        + _ai_excluded_section(regen.ai_excluded)
        + _triage_section(regen.triage)
        + _unresolved_section(regen.unresolved)
        + _unresolved_prs_section(regen.unresolved_prs)
        + _unresolved_backports_section(regen.unresolved_backports)
        + _unresolved_cherry_picks_section(regen.unresolved_cherry_picks)
        + _collided_section(regen.collided)
        + "\n*Generated by valkey-ci-agent. Review before merging into the release line.*"
    )


def _short_sha(sha: str) -> str:
    """Abbreviate a 40-char SHA to 12 for display; pass anything else through.

    A ref that :func:`_compare_ref` could not resolve degrades to the ref name as
    given (not a SHA), so only shorten what looks like a full hex SHA and show a
    non-SHA (or empty) value verbatim as ``unknown``.
    """
    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha.lower()):
        return sha[:12]
    return sha or "unknown"


def _notes_range_lines(rng: "_NotesRange") -> list[str]:
    """Render the resolved range as ``key: value`` lines (shared by body/dry-run).

    Shows the resolved mode, the source and target branches, and both ends of the
    range as ``ref @ <sha>`` so a reviewer can audit the exact commits the notes
    were computed over, not just the branch-model names.
    """
    return [
        f"mode: {rng.mode}",
        f"source_ref: {rng.source_ref}",
        f"target_branch: {rng.target_branch}",
        f"base: {rng.base_ref} @ {_short_sha(rng.base_sha)}",
        f"head: {rng.head_ref} @ {_short_sha(rng.head_sha)}",
    ]


def _notes_range_body_section(rng: Optional["_NotesRange"], regen: Any) -> str:
    """Render the precise notes-range block for the PR body.

    Falls back to the coarse ``base_tag..HEAD`` one-liner only when the resolved
    range could not be captured (``rng is None``), so the body always states the
    span even if SHA resolution was skipped.
    """
    if rng is None:
        return f"- Release notes computed over `{regen.base_tag}..HEAD`.\n"
    block = "\n".join(_notes_range_lines(rng))
    return (
        "- Release notes computed over the range below "
        f"(`{rng.base_ref}..{rng.head_ref}`):\n\n"
        f"```\n{block}\n```\n"
    )


def _rc_warning_section(plan: BranchPlan) -> str:
    """Render the out-of-sequence rc warning into the PR body, if any.

    Returns an empty string when the requested rc is in sequence. When set, the
    warning flags a likely mis-dispatched stage (a re-cut rc, a skipped rc, or
    rc2+ before rc1 exists) so a reviewer can confirm the cut was intended before
    merging it into the release line.
    """
    if not plan.rc_warning:
        return ""
    return (
        "\n### ⚠️ Release candidate out of sequence\n\n"
        f"{plan.rc_warning}\n\n"
        "Cutting anyway as requested. Confirm the dispatched stage is correct "
        "before merging; if not, close this PR and re-dispatch the intended rc.\n"
    )


def _baseline_warning_section(notes_meta: "_NotesMeta", version: str) -> str:
    """Warn when an rc1 of M.0.0 fell back to the nearest tag for its baseline.

    Without a previous-minor release to derive a baseline and without an explicit
    ``--base-ref``, discovery walks back to the nearest reachable tag, which may
    span a whole extra minor of history and over-credit PRs and contributors.
    """
    if not notes_meta.baseline_unanchored:
        return ""
    return (
        "\n### ⚠️ Release-notes baseline is unanchored\n\n"
        f"No `--base-ref` was given for rc1 of {version}, and {version} has no "
        f"previous-minor release to derive one from. The baseline fell back to the "
        f"nearest reachable tag (`{notes_meta.regen.base_tag}`), which may span a "
        f"whole extra minor of history and over-credit PRs and contributors.\n\n"
        "Cutting anyway as requested. Confirm the range above is correct before "
        "merging; if not, close this PR and re-dispatch with an explicit "
        "`--base-ref`.\n"
    )


def _empty_notes_section(notes_meta: "_NotesMeta", plan: BranchPlan) -> str:
    """Explain an empty dated section, keyed on the cause.

    The cut renders only the dated heading + version bump when no bullet survives.
    The already-credited cause has its own section (:func:`_no_new_prs_section`);
    this covers the other two silent causes: an empty range (no PRs), and a range
    whose PRs were all excluded (none labelled ``release-notes`` and AI triage
    judged every label-less one internal-only or could not decide, so none were
    included). Skipped when the section actually carries bullets, or when the
    already-credited drop explains it.
    """
    regen = notes_meta.regen
    if regen.bullet_count or notes_meta.already_credited or notes_meta.security_fixes:
        return ""
    if not regen.had_prs:
        return (
            "\n### Empty release notes\n\n"
            "No merged PRs were found in range, so this cut only adds the dated "
            "heading and the `src/version.h` bump. If you expected notes here, "
            "confirm the range above and that the target branch has the intended "
            "commits.\n"
        )
    if not regen.included:
        return (
            "\n### Empty release notes\n\n"
            "No PR in range was labelled `release-notes`, and AI triage judged the "
            f"{len(regen.ai_excluded)} unlabelled PR(s) internal-only or could not "
            "decide (see the **AI-triaged** / **Needs triage** tables below), so "
            "none were included and the dated section has no bullets. Add the "
            "`release-notes` label to any that should appear and re-cut.\n"
        )
    return ""


def _duplicate_pr_section(duplicate_prs: Sequence[int]) -> str:
    """Flag PRs the model credited in more than one bullet (extra bullets dropped)."""
    if not duplicate_prs:
        return ""
    refs = ", ".join(f"#{n}" for n in duplicate_prs)
    return (
        "\n### ⚠️ A PR was noted more than once\n\n"
        f"The generator emitted more than one bullet for {refs}; only the first "
        "was kept. Review the dated section and confirm the surviving bullet is "
        "the right one before merging.\n"
    )


def _skipped_section(skipped: Sequence[int]) -> str:
    """Flag included PRs the model declined to note, so they don't vanish silently.

    A PR in *skipped* carried the ``release-notes`` label (it was included) but the
    generator produced no bullet for it: it judged the change purely internal or
    non-user-facing, or its output for that PR was lost/unparseable and folded into
    skipped as "what was dropped." Either way the PR is absent from the dated
    section. valkey's ``check_release_notes`` gate is label-only, so a PR the model
    wrongly declined has no other signal; surface it here for a maintainer to
    confirm each is truly not user-facing (or was mislabelled) before merging.
    """
    if not skipped:
        return ""
    refs = ", ".join(f"#{n}" for n in sorted(skipped))
    return (
        "\n### ⚠️ Model declined to note these PRs\n\n"
        f"These PRs carried the `release-notes` label but the generator produced no "
        f"bullet for them, so they are **absent** from the dated section: {refs}. It "
        "judged them purely internal / not user-facing (or its output for them was "
        "lost). Because the label gate is label-only, this is the only signal a "
        "declined PR gets. Confirm each is truly not user-facing (or was "
        "mislabelled); if one should be noted, re-cut after correcting it.\n"
    )


def _uncertain_section(uncertain: Sequence[Any]) -> str:
    """List notes the generator flagged as low-confidence, for a human to confirm.

    Each entry is an :class:`~scripts.release_notes.models.UncertainNote` naming a
    PR the model was unsure about (which category fits, or whether the change is
    user-facing at all). The note is still rendered in the dated section; this
    table asks a maintainer to check the category and wording before merging. A
    non-canonical category the model invented is flagged here too, with the reason
    filled in by the generator.
    """
    if not uncertain:
        return ""
    lines = [
        "",
        "### ⚠️ Notes to double-check",
        "",
        "The generator was not fully confident about these notes. They are included "
        "in the dated section above with its best guess; confirm the category and "
        "wording (or recategorize) before merging:",
        "",
        "| PR | Category | Why flagged |",
        "|----|----------|-------------|",
    ]
    for note in uncertain:
        reason = publish_mod.escape_cell(note.reason) if note.reason else "(no reason given)"
        category = publish_mod.escape_cell(note.category) if note.category else "(none)"
        lines.append(f"| #{note.pr_number} | {category} | {reason} |")
    lines.append("")
    return "\n".join(lines)


def _advisory_section(notes_meta: "_NotesMeta") -> str:
    """Explain the auto-generated Security Fixes and disclaim what could be missed.

    Only rendered when ``--security-from-advisories`` ran (``advisories`` is set).
    Because only published advisories are visible to the token and the version
    match is against author-typed metadata, this always tells a maintainer to
    confirm and to add any embargoed/draft CVEs by hand. When the fetch failed
    (most often a missing advisory-read permission), it says so explicitly rather
    than implying "no security fixes".
    """
    sel = notes_meta.advisories
    if sel is None:
        return ""
    if sel.fetch_failed:
        return (
            "\n### ⚠️ Security advisories could not be read\n\n"
            "`--security-from-advisories` was set, but listing the repository's "
            "security advisories failed (often the token lacks advisory-read "
            f"permission): {publish_mod.escape_cell(sel.fetch_error)}. No CVEs were "
            "auto-added. A maintainer with access should add any Security Fixes by "
            "hand and re-cut.\n"
        )
    lines = [
        "\n### Security fixes (auto-generated from advisories)\n",
    ]
    if sel.matched:
        refs = ", ".join(f"`{f.display_id}`" for f in sel.matched)
        lines.append(
            f"Rendered {len(sel.matched)} published advisory fix(es) matching this "
            f"version into **Security Fixes**: {refs}."
        )
    else:
        lines.append(
            f"No published advisory names this version as a patched version "
            f"({sel.considered} published advisor{'y' if sel.considered == 1 else 'ies'} examined)."
        )
    if sel.unreadable_ids:
        refs = ", ".join(f"`{publish_mod.escape_cell(i)}`" for i in sel.unreadable_ids)
        lines.append(
            f"\n⚠️ {len(sel.unreadable_ids)} published advisor"
            f"{'y' if len(sel.unreadable_ids) == 1 else 'ies'} could **not** be read "
            f"({refs}), so they were neither matched nor ruled out and MAY fix this "
            "version. Check each by hand and add it with `--security-fix` if it applies."
        )
    lines.append(
        "\nOnly **published** advisories are visible here, and the match is on the "
        "advisory's author-entered patched-version, so treat this as a starting "
        "point: confirm the list, and add any embargoed or draft CVEs (and any the "
        "match missed) by hand with `--security-fix` before merging."
    )
    return "\n".join(lines) + "\n"


def _security_dedup_section(notes_meta: "_NotesMeta") -> str:
    """Explain PRs excluded from the generated bullets because supplied as a fix.

    Informational, not a warning: a PR named by a ``--security-fix`` entry is
    dropped from the generated category so the change appears only under **Security
    Fixes** (the hand-authored, separately-reviewed list), avoiding the
    inconsistent double-listing. This just tells the reviewer why the PR is absent
    from its usual category. Returns "" when nothing was deduped.
    """
    prs = notes_meta.security_noted_prs
    if not prs:
        return ""
    refs = ", ".join(f"#{n}" for n in prs)
    subject = "was" if len(prs) == 1 else "were"
    pronoun = "it appears" if len(prs) == 1 else "they appear"
    return (
        "\n### Excluded from generated notes (listed under Security Fixes)\n\n"
        f"{refs} {subject} supplied as a `--security-fix`, so the generated bullet(s) "
        "in the normal categories were dropped to avoid double-listing. To keep the "
        f"notes consistent, {pronoun} only under **Security Fixes**, where each is "
        "reviewed as a factual security entry.\n"
    )


def _security_warning_section(notes_meta: "_NotesMeta") -> str:
    """Warn when ``--urgency SECURITY`` was set with no ``--security-fix`` entries.

    Non-blocking: the release claims security urgency but carries no security
    content, so surface it for the reviewer. (The duplicate-listing case is now
    resolved automatically by dropping the generated bullet; see
    :func:`_security_dedup_section`.)
    """
    if not (notes_meta.urgency.strip().upper() == _SECURITY_URGENCY and not notes_meta.security_fixes):
        return ""
    return (
        "\n### ⚠️ Security fixes need a look\n\n"
        "- Upgrade urgency is **SECURITY** but no `--security-fix` entries were "
        "given, so the release claims security urgency with no security content."
        "\n\nCutting anyway as requested. Confirm before merging; if not, adjust "
        "the `--security-fix` entries or the urgency and re-cut.\n"
    )


def _no_new_prs_section(notes_meta: "_NotesMeta", plan: BranchPlan) -> str:
    """Warn in the PR body when every PR in range was already credited on the line.

    Returns an empty string unless some PR was dropped as a duplicate AND the drop
    left the dated section with no surviving bullets (the common GA-after-final-RC
    case), so the cut is version-bump-only and the reader needs to know the empty
    notes are intentional rather than a generation miss. When the drop removed some
    duplicates but other PRs still produced bullets, the cut ships real notes, so
    this section stays silent (else it would falsely read as "no new notes").
    """
    already_credited = notes_meta.already_credited
    if not already_credited or notes_meta.noted_bullet_count or notes_meta.security_fixes:
        return ""
    refs = ", ".join(f"#{n}" for n in already_credited)
    return (
        "\n### No new release notes\n\n"
        f"Every release-noted PR in range is already credited on `{plan.target}` "
        f"(carried from an earlier cut): {refs}. They were dropped to avoid "
        "duplicate entries, so this cut only adds the dated heading and the "
        "`src/version.h` bump. If you expected new notes here, confirm the new "
        "PRs merged into the target branch and carry the `release-notes` label.\n"
    )


def _ai_triage_reason_cell(pr: Any) -> str:
    """Render the reason cell for an AI-triaged PR, flagging low-confidence calls."""
    reason = publish_mod.escape_cell(pr.reason) if pr.reason else "(no reason given)"
    return f"⚠️ {reason}" if pr.uncertain else reason


def _ai_included_section(ai_included: Sequence[Any]) -> str:
    """Table of label-less PRs AI triage added to the notes, for maintainer review.

    These PRs did not carry the ``release-notes`` label, so valkey's label-only
    gate would have dropped them, but AI triage judged them user-facing and they
    were noted in the dated section above. A maintainer confirms each belongs (or
    removes the note); a ``⚠️`` marks a call the model flagged low-confidence.
    """
    if not ai_included:
        return ""
    lines = [
        "",
        "### AI-triaged into the notes",
        "",
        "These merged PRs were **not** labelled `release-notes`, but AI triage "
        "judged them user-facing and noted them in the dated section above. Confirm "
        "each belongs; if one does not, remove its note before merging:",
        "",
        "| PR | Title | Author | Why included |",
        "|----|-------|--------|--------------|",
    ]
    for pr in ai_included:
        author = f"@{pr.author}" if pr.author else "(unknown)"
        lines.append(
            f"| [#{pr.number}]({pr.url}) | {publish_mod.escape_cell(pr.title)} | "
            f"{author} | {_ai_triage_reason_cell(pr)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _ai_excluded_section(ai_excluded: Sequence[Any]) -> str:
    """Table of label-less PRs AI triage dropped as internal-only, for a sanity check.

    Surfaced so a maintainer can catch a user-facing change the model wrongly
    dropped: these PRs are **absent** from the notes. A ``⚠️`` marks a call the
    model flagged low-confidence. Add the ``release-notes`` label and re-cut to
    pull one back in.
    """
    if not ai_excluded:
        return ""
    lines = [
        "",
        "### AI-triaged out of the notes",
        "",
        "These merged PRs were not labelled `release-notes`, and AI triage judged "
        "them internal-only, so they are **absent** from the notes. Scan for any "
        "user-facing change the model wrongly dropped; label it `release-notes` and "
        "re-cut to include it:",
        "",
        "| PR | Title | Author | Why excluded |",
        "|----|-------|--------|--------------|",
    ]
    for pr in ai_excluded:
        author = f"@{pr.author}" if pr.author else "(unknown)"
        lines.append(
            f"| [#{pr.number}]({pr.url}) | {publish_mod.escape_cell(pr.title)} | "
            f"{author} | {_ai_triage_reason_cell(pr)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _triage_section(triage: Sequence[Any]) -> str:
    """Render a Markdown table of PRs AI triage could not decide, for the PR body."""
    if not triage:
        return ""
    lines = [
        "",
        "### Needs triage",
        "",
        "These merged PRs were not labelled `release-notes`, and AI triage returned "
        "no verdict for them (a parse failure or a dropped entry), so they were "
        "**not** included. A maintainer should decide each: label it `release-notes` "
        "and re-cut to include it, or leave it out.",
        "",
        "| PR | Title | Author |",
        "|----|-------|--------|",
    ]
    for pr in triage:
        author = f"@{pr.author}" if pr.author else "(unknown)"
        lines.append(f"| [#{pr.number}]({pr.url}) | {publish_mod.escape_cell(pr.title)} | {author} |")
    lines.append("")
    return "\n".join(lines)


def _unresolved_section(unresolved: Sequence[Any]) -> str:
    """Flag range commits that resolved to no PR, so a shipped change can't vanish.

    Each entry is an :class:`~scripts.release_notes.models.UnresolvedCommit`: a
    commit in range whose original PR could not be recovered from its subject
    ``(#N)``, an ``## Applied`` table, a ``-x`` cherry-pick trailer, or the
    commit->PR API (a hand-applied cherry-pick whose message was rewritten, or an
    unusual merge). It carries a real change but no PR reference, so it is absent
    from both the dated notes and the triage table above. valkey's gate is
    label-only and keys on PRs, so nothing else surfaces it; list it here for a
    maintainer to identify the change and note it by hand if it is user-facing.
    """
    if not unresolved:
        return ""
    lines = [
        "",
        "### ⚠️ Commits with no resolvable PR",
        "",
        "These commits are in range but could not be tied to a PR (rewritten "
        "cherry-pick, unusual merge, or pre-dating PR history), so they are "
        "**absent** from the notes and the triage table. Confirm whether any is "
        "user-facing and note it by hand if so:",
        "",
        "| Commit | Subject |",
        "|--------|---------|",
    ]
    for commit in unresolved:
        sha = (commit.sha or "")[:12]
        lines.append(f"| `{sha}` | {publish_mod.escape_cell(commit.subject)} |")
    lines.append("")
    return "\n".join(lines)


def _unresolved_prs_section(unresolved_prs: Sequence[Any]) -> str:
    """Flag range commits whose resolved PR could not be fetched, so a shipped change can't vanish.

    Each entry is an :class:`~scripts.release_notes.models.UnresolvedPR`: a range
    commit that resolved to a PR number (from its subject ``(#N)``, an ``## Applied``
    table, a ``-x`` trailer, or the commit->PR API), but fetching that PR returned
    not-found (a moved or deleted PR, an issue number, or a ``(#N)`` from a
    different repo). The change shipped, but with no fetchable PR it is absent from
    both the dated notes and the triage table. valkey's gate is label-only and keys
    on PRs, so nothing else surfaces it; list it here so a maintainer can identify
    the change (the number as written, and the range commit) and note it by hand if
    it is user-facing.
    """
    if not unresolved_prs:
        return ""
    lines = [
        "",
        "### ⚠️ Commits whose PR could not be fetched",
        "",
        "These commits are in range and name a PR, but that PR could not be fetched "
        "(a moved or deleted PR, an issue number, or a `(#N)` from another repo), so "
        "they are **absent** from the notes and the triage table. Confirm whether "
        "any is user-facing and note it by hand if so:",
        "",
        "| Commit | PR referenced |",
        "|--------|---------------|",
    ]
    for pr in unresolved_prs:
        sha = (pr.sha or "")[:12]
        lines.append(f"| `{sha}` | #{pr.number} |")
    lines.append("")
    return "\n".join(lines)


def _unresolved_backports_section(unresolved_backports: Sequence[Any]) -> str:
    """Flag notes credited to a backport PR whose original source was unreachable.

    Each entry is an :class:`~scripts.release_notes.models.UnresolvedBackport`: a
    range commit resolved to a PR that is itself a backport, and discovery could
    not walk it back to the original (no ``## Applied`` table, ``-x`` trailer,
    ``## Backport Summary`` row, recoverable PR-commit ``(#N)``, or
    ``backport/<n>-to-<branch>`` head). The change is noted, but credited to the
    backport PR, not the change's author, and the note reads normally, so nothing
    else in the PR would tip off a reviewer. List it here so a maintainer can find
    the original PR and correct the credit (author and ``(#N)``) before merging.
    """
    if not unresolved_backports:
        return ""
    lines = [
        "",
        "### ⚠️ Notes credited to a backport (original PR not recovered)",
        "",
        "These notes are credited to a **backport** PR because the original PR "
        "that introduced the change could not be recovered. The `(#N)` and author "
        "shown for them are the backport's, not the change's author. Confirm the "
        "original PR and correct the credit before merging:",
        "",
        "| Backport PR | Title |",
        "|-------------|-------|",
    ]
    for bp in unresolved_backports:
        ref = f"[#{bp.number}]({bp.url})" if bp.url else f"#{bp.number}"
        lines.append(f"| {ref} | {publish_mod.escape_cell(bp.title)} |")
    lines.append("")
    return "\n".join(lines)


def _unresolved_cherry_picks_section(unresolved_cherry_picks: Sequence[Any]) -> str:
    """Flag notes whose credit could not be confirmed against their ``-x`` source.

    Each entry is an
    :class:`~scripts.release_notes.models.UnresolvedCherryPick`: a range commit
    carried a ``(cherry picked from commit <sha>)`` trailer, but none of the source
    SHAs resolved through the API (the source commit is not in this repo, a
    hand-applied pick from a fork or history predating PR association), so the note
    was credited from the commit's subject ``(#N)`` or the commit->PR API instead.
    For a rewritten pick that names the PR that landed the change on this line,
    not the change's author; for a preserved-message pick it is correct. The source
    is unreachable, so the two cannot be told apart, and the credited PR carries no
    backport markers, so nothing else in the PR flags it. List it here with the
    source commit so a maintainer can confirm the origin and fix the credit if
    needed before merging.
    """
    if not unresolved_cherry_picks:
        return ""
    lines = [
        "",
        "### ⚠️ Notes with an unconfirmed cherry-pick origin",
        "",
        "These notes come from commits with a `(cherry picked from commit ...)` "
        "trailer whose source commit is not in this repo (a hand-applied pick from "
        "a fork, or history predating PR association), so the credit could not be "
        "confirmed against the original. The credited `(#N)` may be the PR that "
        "landed the change on this line rather than the change's author. Confirm the "
        "origin and correct the credit if needed before merging:",
        "",
        "| Credited PR | Range commit | Subject | Source commit(s) |",
        "|-------------|--------------|---------|------------------|",
    ]
    for cp in unresolved_cherry_picks:
        sha = (cp.sha or "")[:12]
        subj = publish_mod.escape_cell(cp.subject) if cp.subject else ""
        sources = ", ".join(f"`{s[:12]}`" for s in cp.source_shas) or "(none)"
        lines.append(f"| #{cp.number} | `{sha}` | {subj} | {sources} |")
    lines.append("")
    return "\n".join(lines)


def _collided_section(collided: Sequence[Any]) -> str:
    """Flag distinct commits dropped because another commit reused their ``(#N)``.

    Each entry is a :class:`~scripts.release_notes.models.CollidedCommit`: two
    different changes resolved to one PR number via the ambiguous subject
    ``(#N)`` tier (a backport reused a source PR's ``(#N)`` on an unrelated
    follow-up commit), so discovery kept the first and dropped this one. The
    dropped commit resolved to a number, so it is absent from the notes and from
    the other unresolved tables; list it here with the commit that won the number
    so a maintainer can compare the two and note the dropped change by hand if it
    is a separate user-facing change.
    """
    if not collided:
        return ""
    lines = [
        "",
        "### \u26a0\ufe0f Commits dropped by a reused PR number",
        "",
        "Two different commits resolved to the same `(#N)` (a backport reused a "
        "source PR's number on an unrelated commit), so the change below was "
        "**dropped** in favor of the commit that claimed the number first, and is "
        "**absent** from the notes. Confirm whether it is a separate user-facing "
        "change and note it by hand if so:",
        "",
        "| Dropped commit | Subject | Reused # | Kept commit |",
        "|----------------|---------|----------|-------------|",
    ]
    for c in collided:
        sha = (c.sha or "")[:12]
        kept = (c.kept_sha or "")[:12]
        lines.append(
            f"| `{sha}` | {publish_mod.escape_cell(c.subject)} | #{c.number} | `{kept}` |"
        )
    lines.append("")
    return "\n".join(lines)


