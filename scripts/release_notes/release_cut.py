"""Cut a release: generate notes, render a dated section, bump the version.

Each cut generates the notes from the labelled PRs in range and renders them in
one shot. The orchestration reuses the release-format primitives
(``render_release_notes`` from :mod:`release_format`, ``set_version``/
``version_num`` from :mod:`version_bump`, ``list_contributors`` from
:mod:`contributors`); valkey-io/valkey ships no release tooling of its own, so
these modules own the version-macro, dated-section, and contributor-list format
decisions and let the cut run against unmodified upstream ``unstable``.

The release-line branch model (one long-running branch per minor line):

    rc1 of M.m.p   -> create  pre-release-M.m.p  from the source branch
    rcN (N>1)      -> continue pre-release-M.m.p (keeps its prior dated notes)
    GA  of M.m.p   -> create  M.m carrying pre-release-M.m.p's history, then
                      delete pre-release-M.m.p (a rename)
    later patches  -> continue the existing M.m branch

The AI generates the bullets for the range as an in-memory ``{category:
[line, ...]}`` map (the discover/generate/render pipeline); nothing is ever
written to a branch as an "unreleased" block. ``render_release_notes`` renders
that map into a new dated section on the release line, prepends prior RCs' dated
sections, and appends the running contributor list; ``set_version`` separately
bumps ``src/version.h``.

Successive RCs do not double-note. Each cut discovers PRs by graph range from
HEAD back to the most recent reachable RC tag, so a PR captured by rc1's tag is
outside rc2's range. The source branch is never modified. The rendered commit
lands on an agent prep branch that opens a PR into the release line, so the cut
is reviewed before the line advances.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Optional, Sequence

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

# Branch namespace. The release line (pre-release-M.m.p / M.m) is long-running
# and only advanced by merging a PR; the agent never force-pushes it directly.
# The cut's promoted commit lands on a throwaway agent prep branch that PRs into
# the line. The source branch is never modified.
PREP_BRANCH_PREFIX = "agent/release-cut"

_RC_STAGE_RE = re.compile(r"^rc([1-9]\d*)$")
# Matches "Valkey M.m.p-rcN" headings in a running pre-release changelog, to
# tell which rc numbers already shipped on it.
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
    target: str                # branch to write/push, e.g. pre-release-9.1.0 or 9.1
    base_ref: str              # ref the target is (re)based on
    continuing: bool           # True if the target line already exists (drain prior notes)
    rename_from: Optional[str]  # pre-release branch to delete after a GA rename, else None
    rc_warning: Optional[str] = None  # set when the requested rc is out of sequence (surfaced in the PR body)
    branch_warning: Optional[str] = None  # set when the branch-model state looks off (GA dup/orphan, rc-after-GA)


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
    security_dup_prs: Sequence[int]     # PRs noted both as a security fix and a normal bullet
    baseline_unanchored: bool           # rc1 of M.0.0 with no --base-ref (over-broad range risk)
    advisories: Optional[Any] = None    # security.AdvisorySelection when --security-from-advisories ran, else None


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


def resolve_branch_plan(repo_dir: str, *, version: str, stage: str, source_ref: str) -> BranchPlan:
    """Resolve the destination branch and base for this cut.

    Resolves the destination per the release-line branch model: rc stages target
    the long-running ``pre-release-M.m.p``; ``ga`` targets ``M.m`` and, when only
    the rc branch exists, renames it (carry its history, delete the rc branch). An
    existing line is continued (its prior dated sections are drained); otherwise
    it starts from ``source_ref``.
    """
    stage_lc = _normalize_stage(stage)
    major, minor, patch = _split_version(version)
    pre_branch = f"pre-release-{major}.{minor}.{patch}"
    ga_branch = f"{major}.{minor}"

    if stage_lc == "ga":
        ga_exists = _remote_branch_exists(repo_dir, ga_branch)
        pre_exists = _remote_branch_exists(repo_dir, pre_branch)
        if ga_exists and pre_exists:
            # Inconsistent remote state: a prior GA's rename-delete never ran, or
            # M.m was created out of band while the rc line still exists. The GA
            # continue path below would base on M.m and silently leave pre_branch
            # orphaned (the delete is gated on rename_from); worse, M.m may not
            # carry pre_branch's rc history. Refuse rather than orphan/diverge:
            # a PR-body note cannot undo a base_ref already chosen wrong.
            raise ValueError(
                f"GA of {version} found BOTH {pre_branch} and {ga_branch} on origin. "
                f"This is an inconsistent state (a prior GA may have partially run, or "
                f"{ga_branch} was created out of band). Refusing to cut to avoid orphaning "
                f"{pre_branch} and dropping its RC history. Reconcile the branches (delete "
                f"the stray, or confirm {ga_branch} already carries the RC history) and "
                f"re-dispatch."
            )
        if ga_exists:
            warning = _warn_ga_continuation(repo_dir, ga_branch, pre_branch, version)
            return BranchPlan(stage_lc, ga_branch, ga_branch, True, None, None, warning)
        if pre_exists:
            # Carry the rc line's history onto M.m, then delete the rc branch.
            return BranchPlan(stage_lc, ga_branch, pre_branch, True, pre_branch)
        return BranchPlan(stage_lc, ga_branch, source_ref, False, None)

    # rc stages
    if _remote_branch_exists(repo_dir, pre_branch):
        warning = _warn_rc_sequence(repo_dir, pre_branch, stage_lc, major, minor, patch)
        return BranchPlan(stage_lc, pre_branch, pre_branch, True, None, warning)
    # No pre-release line yet. Either this is the first cut (rc1), or the line
    # already went GA and its pre-release branch was deleted by the rename, in
    # which case recreating it from source is almost certainly a mis-dispatch.
    branch_warning = _warn_rc_after_ga(repo_dir, ga_branch, pre_branch, version)
    rc_warning = _warn_rc_first_cut(stage_lc, pre_branch) if branch_warning is None else None
    return BranchPlan(stage_lc, pre_branch, source_ref, False, None, rc_warning, branch_warning)


def _warn_rc_sequence(
    repo_dir: str, pre_branch: str, stage_lc: str, major: int, minor: int, patch: int
) -> Optional[str]:
    """Return a warning (and log it) if a continued rc number is out of sequence.

    A continued rc should be exactly one past the highest rc already recorded on
    the running branch; a repeat (re-cut) or a gap is probably a mis-dispatched
    stage. This only warns; the caller still cuts what was asked. The returned
    message is surfaced in the release PR body so a reviewer sees it too; ``None``
    means the sequence checks out (or could not be read).
    """
    m = _RC_STAGE_RE.match(stage_lc)
    if not m:
        return None
    requested = int(m.group(1))
    try:
        run_git(repo_dir, "fetch", "--quiet", "origin", pre_branch)
        notes = git_output(repo_dir, "show", f"FETCH_HEAD:{NOTES_FILE}")
    except Exception as exc:  # noqa: BLE001 - best-effort; absence just means "no prior rc"
        # resolve_branch_plan already proved the branch exists (ls-remote) and a
        # continuing line always carries 00-RELEASENOTES, so this is realistically a
        # transient fetch failure, not "no prior rc". Log it so a swallowed error is
        # distinguishable from the in-sequence None we return below.
        logger.warning(
            "Could not read %s to check rc sequence (%s); skipping the check.",
            pre_branch, exc,
        )
        return None
    pattern = re.compile(_DATED_RC_RE_TMPL.format(major=major, minor=minor, patch=patch), re.MULTILINE)
    seen = sorted({int(x) for x in pattern.findall(notes)})
    # `highest = max(seen)` keys the expected next rc off the top of the range, so an
    # internal gap (seen == {1, 3}) is NOT flagged when the requested rc is max+1: the
    # cut that created the gap (rc3 onto a line recording only rc1) already fired the
    # "skips ahead" warning below, so re-flagging here would only add noise.
    highest = max(seen) if seen else 0
    expected = highest + 1
    if requested == expected:
        return None
    if requested <= highest:
        detail = (
            f"`{stage_lc}` re-cuts an rc the line already records "
            f"(it lists up to rc{highest}); the next rc should be rc{expected}."
        )
    else:
        detail = (
            f"`{stage_lc}` skips ahead: `{pre_branch}` records up to rc{highest}, "
            f"so the next rc should be rc{expected}."
        )
    logger.warning(
        "Dispatched %s but %s records up to rc%d (expected rc%d). Cutting anyway: "
        "a repeat re-cuts an existing rc; a gap skips one.",
        stage_lc, pre_branch, highest, expected,
    )
    return detail


def _warn_rc_first_cut(stage_lc: str, pre_branch: str) -> Optional[str]:
    """Return a warning (and log it) if rc2+ is dispatched with no pre-release line yet.

    The first cut of a line creates ``pre-release-M.m.p`` and should be rc1.
    rc2+ here means rc1 was never cut (or its branch was lost), almost certainly
    a mis-dispatched stage. Non-blocking: the caller still cuts what was asked.
    """
    m = _RC_STAGE_RE.match(stage_lc)
    if not m or int(m.group(1)) == 1:
        return None
    logger.warning(
        "Dispatched %s but %s does not exist yet (no prior rc on this line). Cutting "
        "anyway as the first cut; expected rc1.",
        stage_lc, pre_branch,
    )
    return (
        f"`{stage_lc}` is the first cut of `{pre_branch}`, but that branch does not "
        f"exist yet: rc1 was never cut (or its line was lost). The first cut of a "
        f"line should be rc1."
    )


def _warn_ga_continuation(
    repo_dir: str, ga_branch: str, pre_branch: str, version: str
) -> Optional[str]:
    """Return a warning (and log it) when a GA continuation looks duplicate or orphaning.

    The GA continue path bases on ``M.m`` and ignores ``pre-release-M.m.p``. Two
    states warrant a heads-up, both non-blocking:

    * The line already records a ``Valkey <version> GA`` dated section: a repeat
      GA stacks a SECOND dated heading for the same version above the existing one.
    * A ``pre-release-M.m.p`` still exists on origin: its ``rcN`` dated sections
      will NOT be carried onto ``M.m`` and the branch is not auto-deleted by this
      continue path.

    Returns ``None`` when neither holds (the normal patch-on-an-existing-line case).
    """
    reasons: list[str] = []

    pre_exists = _remote_branch_exists(repo_dir, pre_branch)
    if pre_exists:
        reasons.append(
            f"a `{pre_branch}` line still exists on origin; its `{version}-rcN` dated "
            f"sections will NOT be carried onto `{ga_branch}`, and that branch is not "
            f"deleted by this run"
        )

    # Read the destination changelog for an already-shipped same-version GA heading,
    # the same fetch + `git show` best-effort pattern _warn_rc_sequence uses.
    try:
        run_git(repo_dir, "fetch", "--quiet", "origin", ga_branch)
        notes = git_output(repo_dir, "show", f"FETCH_HEAD:{NOTES_FILE}")
    except Exception:  # noqa: BLE001 - best-effort; unreadable just means "skip this check"
        notes = ""
    if notes and _ga_heading_present(notes, version):
        reasons.append(
            f"`{ga_branch}` already records a `Valkey {version} GA` dated section; this "
            f"cut adds a SECOND dated heading for the same version above the existing one"
        )

    if not reasons:
        return None
    logger.warning(
        "GA of %s continuing %s looks off: %s. Cutting anyway.",
        version, ga_branch, "; ".join(reasons),
    )
    return ". ".join(r[0].upper() + r[1:] for r in reasons) + "."


def _warn_rc_after_ga(
    repo_dir: str, ga_branch: str, pre_branch: str, version: str
) -> Optional[str]:
    """Return a warning (and log it) when an rc targets a line that already went GA.

    The rc path keys only on ``pre-release-M.m.p``. After a GA rename deleted that
    branch, dispatching a further rc finds it absent and recreates it from source,
    ignoring that ``M.m`` already shipped. Returns ``None`` when ``M.m`` does not
    exist (the first-cut case, handled by :func:`_warn_rc_first_cut`).
    """
    if not _remote_branch_exists(repo_dir, ga_branch):
        return None
    logger.warning(
        "rc of %s targets %s, which is absent, but %s already exists as a GA line. "
        "Recreating the pre-release branch from source. Cutting anyway.",
        version, pre_branch, ga_branch,
    )
    return (
        f"`{ga_branch}` already exists as a GA line, but this rc targets `{pre_branch}`, "
        f"which was deleted during the GA rename. This cut recreates that pre-release "
        f"branch from source. A further patch should normally be dispatched as the next "
        f"patch version (continuing `{ga_branch}`), not an rc of {version}."
    )


def _ga_heading_present(notes_text: str, version: str) -> bool:
    """True if *notes_text* already carries a ``Valkey <version> GA`` dated heading."""
    pattern = re.compile(
        r"^Valkey\s+" + re.escape(version) + r"\s+GA\b", re.MULTILINE
    )
    return bool(pattern.search(notes_text))


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
    token: Optional[str],
    security_fixes: Optional[Sequence[str]],
) -> tuple[str, str]:
    """Render *grouped* onto the destination changelog and bump the version.

    Returns ``(new_dest_notes, new_version_h)``. ``render_release_notes`` renders
    the categorized bullets into a new dated section atop the destination's
    running changelog (``dest_notes_text``, empty on a first cut), and
    ``set_version`` rewrites the three version macros. The contributor list is
    generated over ``contrib_base..HEAD`` and merged into the cumulative footer.
    *valkey_clone_dir* is needed for the git range resolution behind the
    contributor lookup, not to load any format code.
    """
    contributors: list[str] = []
    if contrib_base:
        # Resolve both ends to SHAs the GitHub compare API accepts. contrib_base
        # is typically a remote-tracking ref (origin/unstable) and the head is the
        # literal "HEAD"; both 404 the API and silently fall back to git shortlog
        # (names only, no @handle, bots not filtered). See _compare_ref.
        base_sha = _compare_ref(valkey_clone_dir, contrib_base)
        head_sha = _compare_ref(valkey_clone_dir, "HEAD")
        contributors = gc.list_contributors(
            repo_full_name, base_sha, head_sha, token, repo_dir=valkey_clone_dir
        )
        logger.info("Collected %d contributor(s) over %s..HEAD", len(contributors), contrib_base)
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
    tag, then root commit. The contributor list must span the same range as the
    bullets, or the credits diverge from the notes. So whenever the notes
    baseline was pinned (``notes_base_ref``, an explicit ``--base-ref`` or rc1's
    derived previous release), it is used here before ``git describe``: on a
    branch following valkey's fork-at-freeze model, ``describe`` returns a far
    older nearest tag (e.g. 8.0.8 from unstable) than the real baseline (9.0.0),
    crediting a whole extra minor of history. The describe/root fallbacks remain
    for the tag-resolved path (rc2+/ga), where the notes baseline is a tag and
    ``notes_base_ref`` is None. The describe/root chain keeps the
    ``### Contributors`` list from being silently empty.
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
    try:
        roots = git_output(repo_dir, "rev-list", "--max-parents=0", "HEAD").split("\n")
        roots = [r for r in roots if r.strip()]
        if roots:
            return roots[-1].strip()
    except Exception:  # noqa: BLE001
        pass
    return None


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


def _credited_pr_numbers(notes_text: str) -> set[int]:
    """Return the PR numbers a release-line changelog already credits.

    Reads every bullet line's trailing ``(#N)`` from *notes_text* (a destination
    changelog: the dated sections of pre-release-M.m.p or M.m). This is the dedup
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
    credits (dedup) and with the ``--security-fix`` refs (double-listing check).
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


def _security_dup_prs(
    security_fixes: Optional[Sequence[str]], noted: set[int]
) -> list[int]:
    """Return PR numbers credited both as a ``--security-fix`` and a normal bullet.

    Reads each security entry's trailing ``(#N)`` (the same canonical reference the
    notes use) and intersects with *noted* (the PRs this cut renders as normal
    bullets). A match means the change is listed twice; the caller flags it for the
    reviewer rather than dropping either, since a maintainer may intend both.
    """
    if not security_fixes:
        return []
    dup: list[int] = []
    for entry in security_fixes:
        m = _TRAILING_PR_RE.search(entry)
        if m and int(m.group(1)) in noted and int(m.group(1)) not in dup:
            dup.append(int(m.group(1)))
    return dup


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
    source_ref: str,
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
) -> int:
    """Cut a release: generate notes with AI, render onto the release line, open PRs.

    ``source_clone_dir`` is a clone of the source branch; it doubles as
    ``valkey_clone_dir`` for the contributor range lookup. The destination
    release branch is materialized in a worktree under it. Returns 0 on success,
    1 on failure.

    When *security_from_advisories* is set, published GitHub repository advisories
    fixed by *version* are rendered into the Security Fixes section (merged with
    any manual ``--security-fix`` entries, which win on CVE collision). See
    :mod:`scripts.release_notes.security` for why this is a version-string match,
    not the PR graph-walk, and why embargoed advisories are surfaced as a
    reviewer disclaimer rather than auto-included.
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
        source_clone_dir, version=version, stage=stage, source_ref=source_ref
    )
    logger.info(
        "Plan: stage=%s target=%s base=%s continuing=%s rename_from=%s",
        plan.stage, plan.target, plan.base_ref, plan.continuing, plan.rename_from or "<none>",
    )

    # A continuing cut (rc2+, or a GA draining the rc line) has no reachable RC
    # tag to anchor discovery on: this workflow never pushes RC tags and the fork
    # carries none, so the rc2+ default `--match <version>-rc*` glob makes
    # `git describe` fail with "no tag reachable" and aborts the cut. The branch
    # plan already knows the continuing baseline (pre-release-M.m.p, or M.m for a
    # GA continuation), which resolves as origin/<name> in the clone, so use it as
    # the range baseline and drop the glob. A wider walk here is corrected
    # downstream: any PR the line already shipped is removed against the
    # destination changelog (see already_credited below). An explicit --base-ref
    # always wins, and it is the only way base_ref is non-None on a continuing cut
    # (the rc1 default fires only when the line does not yet exist), so rc1 first
    # cuts keep their derived-base / nearest-tag fallback untouched.
    notes_base_ref, notes_tag_glob = base_ref, tag_glob
    if base_ref is None and plan.continuing:
        notes_base_ref, notes_tag_glob = plan.base_ref, None
        logger.info(
            "Continuing cut with no explicit --base-ref; anchoring discovery to the "
            "release line base %r (no RC tag is reachable on the fork).",
            plan.base_ref,
        )

    # 1. Generate the categorized bullets for the range from labelled PRs.
    regen = pipeline_mod.regenerate_unreleased(
        repo, source_clone_dir, head_ref=source_ref,
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
    run_git(source_clone_dir, "fetch", "origin", plan.base_ref, env=git_env)
    prep_branch = f"{PREP_BRANCH_PREFIX}/{version}-{plan.stage}"
    dest_dir = os.path.join(source_clone_dir, ".release-dest")
    run_git(source_clone_dir, "worktree", "add", "--force", "-B", prep_branch, dest_dir,
            f"origin/{plan.base_ref}")
    try:
        # A first cut of a line has no prior dated changelog to prepend.
        dest_notes_path = os.path.join(dest_dir, NOTES_FILE)
        dest_notes_text = _read(dest_notes_path) if plan.continuing else ""
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
        # in this cut means the same change is listed twice (Security Fixes + its
        # category). Flag for the reviewer; do not auto-drop (a maintainer may want
        # both). Match against the PRs actually noted now (grouped post-drop).
        security_dup_prs = _security_dup_prs(
            security_fixes, _grouped_pr_numbers(grouped)
        )

        # 3. Render bullets -> dated section on dest; bump version.h.
        new_dest_notes, new_version = promote_and_bump(
            valkey_clone_dir,
            grouped=grouped,
            dest_notes_text=dest_notes_text,
            dest_version_text=dest_version_text,
            version=version, stage_lc=plan.stage, urgency=urgency, date=date,
            repo_full_name=repo_full_name, contrib_base=contrib_base, token=token,
            security_fixes=security_fixes,
        )

        # Count what survives into the dated section after the already-credited
        # drop. When some PRs were dropped as duplicates but others remain, this
        # is > 0 and the cut still ships real notes; only when it is 0 is the cut
        # version-bump-only. The "No new release notes" section keys on this so a
        # cut that drops a duplicate yet adds a new note is not mislabelled empty.
        noted_bullet_count = sum(len(lines) for lines in grouped.values())

        notes_meta = _NotesMeta(
            regen=regen, already_credited=already_credited,
            noted_bullet_count=noted_bullet_count, urgency=urgency,
            security_fixes=security_fixes, security_dup_prs=security_dup_prs,
            baseline_unanchored=baseline_unanchored, advisories=advisories,
        )

        if dry_run:
            _print_dry_run(plan, version, new_dest_notes, new_version, notes_meta)
            return 0

        # 4. Ensure the release line exists to PR into. When starting a new line
        #    (rc1, first GA, or a GA rename carrying the rc history), create it at
        #    origin/<base_ref> with a non-force push so a race can't clobber it.
        created_line = False
        created_line_oid = ""
        if not _remote_branch_exists(source_clone_dir, plan.target):
            # Record the OID we create the line at (the tip of origin/<base_ref>,
            # which the non-force push copies onto the new ref). A rollback below
            # deletes the line only under a lease on this OID, so it can never
            # remove a commit a concurrent writer advanced the line to after we
            # created it. If we cannot read the OID, the rollback refuses to delete
            # (see _rollback_created_line) rather than risk a blind delete.
            try:
                created_line_oid = git_output(
                    source_clone_dir, "rev-parse", "--verify",
                    f"origin/{plan.base_ref}^{{commit}}",
                ).strip()
            except Exception:  # noqa: BLE001 - no OID -> rollback stays conservative
                created_line_oid = ""
            run_git(source_clone_dir, "push", "origin",
                    f"origin/{plan.base_ref}:refs/heads/{plan.target}", env=git_env)
            created_line = True
            logger.info("Created release line %s at origin/%s (%s)",
                        plan.target, plan.base_ref, created_line_oid[:12] or "unknown-oid")

        try:
            # 5. Commit the rendered notes + bumped version on the prep branch, push
            #    it (agent-namespaced, force-with-lease), and PR it into the line. The
            #    source branch is never modified, so no companion PR. Each cut
            #    rediscovers PRs from the last RC tag, so prior RCs' PRs are excluded
            #    by the graph range.
            _write(dest_notes_path, new_dest_notes)
            _write(os.path.join(dest_dir, VERSION_FILE), new_version)
            release_url = _commit_push_release_pr(
                repo, dest_dir, repo_full_name=repo_full_name, plan=plan,
                version=version, prep_branch=prep_branch, notes_meta=notes_meta,
                git_env=git_env,
            )
            # Log the PR before the rename cleanup so a delete failure below still
            # leaves the created PR's URL in the CI log.
            logger.info("Release PR: %s", release_url)

            # 6. GA rename: delete the old pre-release branch. The M.m line was created
            #    from it above, so its history is already carried. A branch already
            #    gone is fine; a delete that fails with the branch still on origin
            #    raises (both branches present is what the next GA hard-refuses).
            if plan.rename_from:
                _delete_remote_branch(source_clone_dir, plan.rename_from, git_env)
        except Exception:
            # The release line is mutated (step 4) before the prep branch, PR, and
            # GA-rename delete are known-good (steps 5-6). If any of those fail, a
            # line THIS run just created would be left stranded: for a GA rename,
            # stranding M.m alongside pre-release-M.m.p, exactly the inconsistent
            # state resolve_branch_plan hard-refuses on the next GA, forcing a
            # manual reconcile. Roll back only a line we created (a pre-existing or
            # continued line is never touched), restoring the single-branch state a
            # retry expects. A rollback delete that itself fails is logged; we
            # re-raise the original failure regardless so the run still exits
            # non-zero.
            if created_line:
                logger.warning(
                    "Release cut failed after creating %s; rolling it back so the "
                    "release line is not left inconsistent.", plan.target,
                )
                try:
                    _rollback_created_line(
                        source_clone_dir, plan.target, created_line_oid, git_env
                    )
                except Exception:  # noqa: BLE001 - surface the original failure, not the rollback's
                    logger.exception("Rollback of %s failed; delete it manually.", plan.target)
            raise

        return 0
    finally:
        run_git(source_clone_dir, "worktree", "remove", "--force", dest_dir)


def _print_dry_run(plan, version, dest_notes, version_h, notes_meta: "_NotesMeta") -> None:
    regen = notes_meta.regen
    print(f"\n===== release plan ({version} {plan.stage}) =====")
    print(f"target branch: {plan.target}  base: {plan.base_ref}  continuing: {plan.continuing}")
    # The resolved discovery range (regen.base_tag..HEAD) is the actual span the
    # notes were computed over; plan.base_ref is the branch-model base, which can
    # differ (e.g. nearest-tag fallback). Surface it so an over-broad range shows.
    print(f"notes range: {regen.base_tag}..HEAD")
    if notes_meta.baseline_unanchored:
        print(f"⚠️  baseline unanchored: rc1 of {version} fell back to nearest tag {regen.base_tag!r}")
    if plan.rc_warning:
        print(f"⚠️  rc out of sequence: {plan.rc_warning}")
    if plan.branch_warning:
        print(f"⚠️  branch-model: {plan.branch_warning}")
    if plan.rename_from:
        print(f"GA rename: would delete {plan.rename_from}")
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
    if notes_meta.security_dup_prs:
        print(f"⚠️  security fix also noted normally: {list(notes_meta.security_dup_prs)}")
    if notes_meta.urgency.strip().upper() == _SECURITY_URGENCY and not notes_meta.security_fixes:
        print("⚠️  urgency SECURITY but no security-fix entries")
    if regen.triage:
        print(f"triage PRs (untagged): {[p.number for p in regen.triage]}")
    if regen.unresolved:
        print(f"⚠️  commits with no resolvable PR: {[c.sha[:12] for c in regen.unresolved]}")
    if regen.unresolved_backports:
        print("⚠️  notes credited to a backport (original PR not recovered): "
              f"{[bp.number for bp in regen.unresolved_backports]}")
    print(f"\n===== {NOTES_FILE} (release branch, dry run) =====\n{dest_notes}")
    print(f"\n===== {VERSION_FILE} (dry run) =====\n{version_h}")


def _commit_push_release_pr(
    repo: Any, dest_dir: str, *, repo_full_name: str, plan: BranchPlan, version: str,
    prep_branch: str, notes_meta: "_NotesMeta", git_env: dict[str, str],
) -> str:
    """Commit the cut on the prep branch, push it, and open/update a PR into the line.

    The PR is ``head=prep_branch`` into ``base=plan.target`` (the release line),
    so it shows exactly the promoted diff and merges into the line, never the
    self-referential merge-back-into-source shape the release line must avoid.
    The prep branch is agent-namespaced, so force-with-lease on it is safe.
    *notes_meta* carries the advisories surfaced in the body (out-of-sequence rc,
    branch-model anomalies, unanchored baseline, empty/duplicate notes, security
    correlations, triage PRs).
    """
    run_git(dest_dir, "config", "user.name", BOT_NAME)
    run_git(dest_dir, "config", "user.email", BOT_EMAIL)
    run_git(dest_dir, "add", NOTES_FILE, VERSION_FILE)
    run_git(dest_dir, "commit", "-s", "-m", commit_title(version, plan.stage))
    if not prep_branch.startswith(f"{PREP_BRANCH_PREFIX}/"):
        raise RuntimeError(f"Refusing to push to non-namespaced prep branch: {prep_branch!r}")
    # Give --force-with-lease a valid basis. The fresh `git clone --branch <source_ref>`
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

    title = commit_title(version, plan.stage)
    body = _build_pr_body(plan, version, notes_meta)
    existing = publish_mod.find_existing_pr(
        repo, base_repo=repo_full_name, push_repo=None, branch=prep_branch
    )
    return publish_mod.open_or_update_pr(
        repo, base_repo=repo_full_name, push_repo=None, branch=prep_branch,
        base_branch=plan.target, title=title, body=body, existing=existing,
    )


def _build_pr_body(plan: BranchPlan, version: str, notes_meta: "_NotesMeta") -> str:
    """Assemble the release PR body: summary line, then each advisory section.

    Sections are appended in a fixed, reviewer-friendly order: the most actionable
    "is this the right cut?" warnings (sequence, branch model, baseline) first,
    then the "why do the notes look like this?" explanations (empty, duplicate,
    security), then the triage table. Each section helper returns "" when it does
    not apply, so the body stays quiet on a clean cut.
    """
    regen = notes_meta.regen
    return (
        f"Cuts **{stage_release_name(version, plan.stage)}** onto release line "
        f"`{plan.target}`.\n\n"
        f"- Promotes the release notes into a dated section, bumps "
        f"`src/version.h`, and refreshes the running contributor list.\n"
        f"- Release notes computed over `{regen.base_tag}..HEAD`.\n"
        + (f"- GA: carries `{plan.rename_from}`'s history; that branch is deleted by this run.\n"
           if plan.rename_from else "")
        + _rc_warning_section(plan)
        + _branch_warning_section(plan)
        + _baseline_warning_section(notes_meta, version)
        + _empty_notes_section(notes_meta, plan)
        + _no_new_prs_section(notes_meta, plan)
        + _duplicate_pr_section(regen.duplicate_prs)
        + _skipped_section(regen.skipped)
        + _uncertain_section(regen.uncertain)
        + _advisory_section(notes_meta)
        + _security_warning_section(notes_meta)
        + _triage_section(regen.triage)
        + _unresolved_section(regen.unresolved)
        + _unresolved_backports_section(regen.unresolved_backports)
        + "\n*Generated by valkey-ci-agent. Review before merging into the release line.*"
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


def _branch_warning_section(plan: BranchPlan) -> str:
    """Render a branch-model anomaly warning (GA dup/orphan, rc-after-GA), if any."""
    if not plan.branch_warning:
        return ""
    return (
        "\n### ⚠️ Release line state looks off\n\n"
        f"{plan.branch_warning}\n\n"
        "Cutting anyway as requested. Confirm the dispatched version/stage is "
        "correct before merging; if not, close this PR and reconcile the release "
        "line.\n"
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
    this covers the other two silent causes: an empty range (no PRs), and
    a range whose every PR needs triage (so none were included). Skipped when the
    section actually carries bullets, or when the already-credited drop explains it.
    """
    regen = notes_meta.regen
    if regen.bullet_count or notes_meta.already_credited:
        return ""
    if not regen.had_prs:
        return (
            "\n### Empty release notes\n\n"
            "No merged PRs were found in range, so this cut only adds the dated "
            "heading and the `src/version.h` bump. If you expected notes here, "
            "confirm the range above and that the source branch has the intended "
            "commits.\n"
        )
    if regen.triage:
        return (
            "\n### Empty release notes\n\n"
            f"All {len(regen.triage)} PR(s) in range are unlabelled or "
            "double-labelled (see **Needs triage** below), so none were included "
            "and the dated section has no bullets. Label them and re-cut if they "
            "should appear.\n"
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
    Because only *published* advisories are visible to the token and the version
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


def _security_warning_section(notes_meta: "_NotesMeta") -> str:
    """Render security-fix correlation warnings: duplicate listing, urgency mismatch.

    Two independent, non-blocking checks share one section:

    * a ``--security-fix`` whose ``(#N)`` is also a normal release-noted bullet, so
      the change appears twice; and
    * ``--urgency SECURITY`` with no ``--security-fix`` entries, so the release
      claims security urgency with no security content.
    """
    lines: list[str] = []
    if notes_meta.security_dup_prs:
        refs = ", ".join(f"#{n}" for n in notes_meta.security_dup_prs)
        lines.append(
            f"- {refs} is listed both as a `--security-fix` and as a normal "
            "release-noted bullet, so it appears under **Security Fixes** and its "
            "category in this cut."
        )
    if notes_meta.urgency.strip().upper() == _SECURITY_URGENCY and not notes_meta.security_fixes:
        lines.append(
            "- Upgrade urgency is **SECURITY** but no `--security-fix` entries were "
            "given, so the release claims security urgency with no security content."
        )
    if not lines:
        return ""
    return (
        "\n### ⚠️ Security fixes need a look\n\n"
        + "\n".join(lines)
        + "\n\nCutting anyway as requested. Confirm before merging; if not, adjust "
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
    if not already_credited or notes_meta.noted_bullet_count:
        return ""
    refs = ", ".join(f"#{n}" for n in already_credited)
    return (
        "\n### No new release notes\n\n"
        f"Every release-noted PR in range is already credited on `{plan.target}` "
        f"(carried from an earlier cut): {refs}. They were dropped to avoid "
        "duplicate entries, so this cut only adds the dated heading and the "
        "`src/version.h` bump. If you expected new notes here, confirm the new "
        "PRs merged into the source branch and carry the `release-notes` label.\n"
    )


def _triage_section(triage: Sequence[Any]) -> str:
    """Render a Markdown table of untagged/double-labelled PRs for the PR body."""
    if not triage:
        return ""
    lines = [
        "",
        "### Needs triage",
        "",
        "These merged PRs in range carry neither `release-notes` nor "
        "`no-release-notes` (or carry both) and were not included. A maintainer "
        "should label them:",
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


def _unresolved_backports_section(unresolved_backports: Sequence[Any]) -> str:
    """Flag notes credited to a backport PR whose original source was unreachable.

    Each entry is an :class:`~scripts.release_notes.models.UnresolvedBackport`: a
    range commit resolved to a PR that is itself a backport, and discovery could
    not walk it back to the original (no ``## Applied`` table, ``-x`` trailer,
    ``## Backport Summary`` row, recoverable PR-commit ``(#N)``, or
    ``backport/<n>-to-<branch>`` head). The change *is* noted, but credited to the
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


def _rollback_created_line(
    repo_dir: str, branch: str, expected_oid: str, git_env: dict[str, str]
) -> None:
    """Delete a release line this run created, but only if it still points at *expected_oid*.

    The rollback fires when a cut failed after creating the line (step 4) but
    before the prep branch / PR / GA-rename were known-good. Between the create
    and this rollback another writer may have advanced the line (a human merge, a
    concurrent cut); a plain ``push --delete`` would then destroy their commit.
    Guard the delete with ``--force-with-lease=<ref>:<oid>`` pinned to the OID we
    created the line at, so the delete is accepted only while the line is still
    exactly the commit we put there, and is rejected (leaving the branch intact) if
    it moved. When *expected_oid* is empty (we could not read it at create time),
    refuse to delete at all rather than delete blind: a stranded line the next GA
    flags is recoverable, a wrongly deleted commit is not.
    """
    if not expected_oid:
        logger.warning(
            "Not rolling back %s: the OID it was created at is unknown, so a delete "
            "could remove a commit added after creation. Delete it manually if it is "
            "the stray line this run created.", branch,
        )
        return
    try:
        run_git(repo_dir, "push",
                f"--force-with-lease=refs/heads/{branch}:{expected_oid}",
                "origin", "--delete", branch, env=git_env)
        logger.info("Rolled back run-created release line %s (was at %s)",
                    branch, expected_oid[:12])
        return
    except Exception as exc:  # noqa: BLE001
        # A lease rejection ("stale info") means the line advanced past the commit
        # we created it at: another writer owns the current tip, so leaving it in
        # place is correct. Distinguish that (branch still present, but not ours to
        # delete) from a delete that simply did not happen.
        try:
            still_present = _remote_branch_exists(repo_dir, branch)
        except Exception:  # noqa: BLE001
            still_present = True
        if still_present:
            logger.warning(
                "Did not roll back %s: it no longer points at the commit this run "
                "created (%s), so it was advanced by another writer and is left "
                "intact (%s). Reconcile manually if needed.",
                branch, expected_oid[:12], exc,
            )
        else:
            logger.info("Release line %s already gone; rollback was a no-op: %s", branch, exc)
        return


def _delete_remote_branch(repo_dir: str, branch: str, git_env: dict[str, str]) -> None:
    """Delete a remote branch, tolerating one that is already gone.

    A branch that no longer exists on origin is the desired end state, so a
    delete that fails for that reason is fine. But a delete that fails while the
    branch is still on origin must not pass as success: for a GA rename that
    leaves both ``pre-release-M.m.p`` and ``M.m`` on origin, that is precisely the
    inconsistent state the next GA of that line hard-refuses (see
    ``resolve_branch_plan``). Confirm the branch is gone before treating a
    failure as benign; otherwise raise so the caller returns non-zero.
    """
    try:
        run_git(repo_dir, "push", "origin", "--delete", branch, env=git_env)
        logger.info("Deleted remote branch %s (GA rename)", branch)
        return
    except Exception as exc:  # noqa: BLE001
        try:
            still_present = _remote_branch_exists(repo_dir, branch)
        except Exception:  # noqa: BLE001 - can't confirm; assume the worst (still there)
            still_present = True
        if not still_present:
            logger.info("Remote branch %s already gone; delete was a no-op: %s", branch, exc)
            return
        raise RuntimeError(
            f"Failed to delete {branch} during the GA rename and it still exists on "
            f"origin ({exc}). Both {branch} and the release line are now present, which "
            f"the next GA of this line refuses as an inconsistent state. Delete {branch} "
            f"manually to reconcile."
        ) from exc
