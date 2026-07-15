"""Entry point for the AI release-notes cut.

Driven by ``workflow_dispatch``: a maintainer supplies the source branch
(``--head-ref``) and the target ``--version``/``--stage``/``--urgency``, and the
agent cuts the release in one shot. Nothing accumulates notes on a branch; the
notes for a release are generated all at once from the labelled PRs in range,
rendered into a dated section, and the version is bumped.

Pipeline: clone valkey (full depth + tags), :mod:`discover` the range (the
``release-notes``-labelled PRs from HEAD back to the most recent reachable RC
tag), :mod:`generate` bullets via Claude/Bedrock, then :mod:`release_cut`
renders them onto the release line (dated section + ``src/version.h`` bump +
running contributor list, draining prior RCs) and opens the PR.

Returns 0 on success or a benign no-op (empty range), 1 on failure, and 2 on a
usage error (argparse). Orchestration is wrapped so a GitHub/AI error is logged
and surfaced as a non-zero exit rather than an uncaught crash.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github import Auth, Github

from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.github_client import retry_github_call
from scripts.common.proc import git_output, run_git
from scripts.release_notes import discover as discover_mod
from scripts.release_notes import release_cut as cut_mod

logger = logging.getLogger(__name__)

# Upgrade-urgency values render_release_notes() accepts, mirrored here so a bogus
# value fails at argparse (exit 2) rather than deep in rendering after a wasted
# clone + AI run. Kept in sync with the workflow's `urgency` choice list and the
# valkey format module's VALID_URGENCIES.
_VALID_URGENCIES = ("LOW", "MODERATE", "HIGH", "CRITICAL", "SECURITY")

# Config via env so the workflow can pass GitHub Actions context directly; the
# RELEASE_NOTES_ prefix mirrors the CI_FIX_/FUZZER_ convention. These are read at
# argparse-build time inside main() (not captured as import-time module
# constants), so a test can monkeypatch the environment before calling main().
# An import-time read would freeze the value at first import, out of any test's
# reach and dependent on the ambient env when the module first loaded.
_DEFAULT_REPO = "valkey-io/valkey"


def _token() -> str:
    """Resolve the GitHub token: env chain (CLI override applied in main)."""
    return (
        os.environ.get("RELEASE_NOTES_GITHUB_TOKEN", "")
        or os.environ.get("TARGET_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
    )


def _env_flag(name: str) -> bool:
    """True if env var *name* holds a truthy string ('true'/'1'/'yes').

    Boolean workflow inputs arrive as the literal strings ``'true'``/``'false'``
    (see the ``RELEASE_NOTES_*`` exports), so a bare ``bool(os.environ.get(...))``
    would read ``'false'`` as truthy. Parse the string explicitly.
    """
    return os.environ.get(name, "").strip().lower() in {"true", "1", "yes"}


def _default_tag_glob(version: str, stage: str) -> str | None:
    """Derive the baseline-tag match glob for this cut, or None.

    Baseline tag resolution picks the highest-version reachable tag, but without
    a glob it considers tags from *every* release line, so after a cross-line
    merge a sibling line's tag can win. A glob pins candidates to the intended
    line. The boundary depends on the stage:

    * rc2+: the prior RC of this version, ``<version>-rc*`` (so a cut of
      9.1.0-rc3 walks back only to 9.1.0-rc2).
    * ga: this line's tags, ``M.m.*`` (so a patch GA of 8.1.9 walks back to the
      last 8.1.x tag and can never pick up a concurrent 8.2.x line's tag). A
      first GA of a new minor/major is anchored to its pre-release branch instead
      and drops this glob (see release_cut.py), so scoping it here is safe.
    * rc1 / anything else: ``None``. rc1 has no prior same-version RC to anchor
      to (there is no rc0), and its true baseline is the previous release, which
      is not reachable from the source branch in valkey's fork-at-freeze model.
      So rc1 does not resolve a tag from the source branch; it anchors to the
      previous release tag resolved from the repo's tags instead (see
      :func:`discover.resolve_previous_release_tag`, invoked from
      :func:`_run_cut`).

    A version that is not ``M.m.p`` also returns None.
    """
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not m:
        return None
    stage_lc = stage.strip().lower()
    rc = re.fullmatch(r"rc([1-9]\d*)", stage_lc)
    if rc and int(rc.group(1)) >= 2:
        return f"{version.strip()}-rc*"
    if stage_lc == "ga":
        return f"{m.group(1)}.{m.group(2)}.*"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=_token(), help="GitHub token (App installation or PAT)")
    parser.add_argument("--repo", default=os.environ.get("RELEASE_NOTES_REPO", _DEFAULT_REPO),
                        help="Target repo, owner/name")
    parser.add_argument("--head-ref", default=os.environ.get("RELEASE_NOTES_HEAD_REF", ""),
                        help="Source branch whose merged PRs are cut, e.g. unstable "
                             "(a short branch/tag name, passed to `git clone --branch`)")
    parser.add_argument("--version", default=os.environ.get("RELEASE_NOTES_VERSION", ""),
                        help="Target version MAJOR.MINOR.PATCH, e.g. 9.1.0")
    parser.add_argument("--stage", default=os.environ.get("RELEASE_NOTES_STAGE", ""),
                        help="Release stage: rc1..rcN or ga")
    parser.add_argument("--urgency", default=os.environ.get("RELEASE_NOTES_URGENCY", ""),
                        help="Upgrade urgency: LOW, MODERATE, HIGH, CRITICAL, SECURITY")
    parser.add_argument("--date", default=os.environ.get("RELEASE_NOTES_DATE", ""),
                        help="Release date YYYY-MM-DD (default: today)")
    parser.add_argument("--tag-glob", default=os.environ.get("RELEASE_NOTES_TAG_GLOB", ""),
                        help="Optional --match glob restricting the baseline tag, e.g. '9.1.0-rc*'")
    parser.add_argument("--base-ref", default=os.environ.get("RELEASE_NOTES_BASE_REF", ""),
                        help="Explicit baseline ref (branch/tag/SHA) overriding tag resolution. "
                             "Use when the line has no reachable tag, e.g. a fork.")
    parser.add_argument("--contrib-base-ref", default=os.environ.get("RELEASE_NOTES_CONTRIB_BASE", ""),
                        help="Contributor range start (default: last tag, else root commit)")
    parser.add_argument("--security-fix", action="append", default=None, dest="security_fixes",
                        help="A Security Fixes bullet (repeatable)")
    parser.add_argument("--security-from-advisories", action="store_true",
                        default=_env_flag("RELEASE_NOTES_SECURITY_FROM_ADVISORIES"),
                        help="Auto-render PUBLISHED GitHub security advisories fixed by this "
                             "version into Security Fixes (merged with any --security-fix bullets)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and print the cut without pushing or opening a PR")
    parser.add_argument("--force-ready", action="store_true",
                        default=_env_flag("RELEASE_NOTES_FORCE_READY"),
                        help="Open the release PR ready for review even when the cut raised "
                             "reviewer-facing signals. By default such a cut opens as a draft "
                             "(the merge is held) until a maintainer resolves the flagged items "
                             "and marks it ready.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.token:
        parser.error("a GitHub token is required (--token or RELEASE_NOTES_GITHUB_TOKEN/GITHUB_TOKEN)")
    if not args.head_ref:
        parser.error("--head-ref (source branch, e.g. unstable) is required")
    if not (args.version and args.stage and args.urgency):
        parser.error("--version, --stage, and --urgency are required")

    # Fail fast on malformed inputs, before the expensive clone + AI run. Each of
    # these is otherwise first rejected deep in the pipeline (or never), surfacing
    # as an opaque exit-1 traceback after work was wasted, or silently corrupting
    # output (a non-canonical version leaks into version.h while the branch name is
    # normalized). Validate (and canonicalize the version) here for a clear exit-2.
    try:
        version = cut_mod.canonical_version(args.version)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        stage = cut_mod._normalize_stage(args.stage)
    except ValueError as exc:
        parser.error(str(exc))
    urgency = args.urgency.strip().upper()
    if urgency not in _VALID_URGENCIES:
        parser.error(f"--urgency must be one of {', '.join(_VALID_URGENCIES)}, got {args.urgency!r}")
    if args.date and not _is_iso_date(args.date):
        parser.error(f"--date must be ISO YYYY-MM-DD (e.g. 2026-06-30), got {args.date!r}")

    base_ref = args.base_ref or None

    # rc1 has no prior same-version RC tag and, in valkey's fork-at-freeze model,
    # no release tag reachable from the source branch (tags live on the release
    # branches, never on unstable), so tag resolution from the source branch cannot
    # find rc1's baseline. rc1's true baseline is the previous release. When the
    # user passed neither --base-ref nor --tag-glob, defer to a repo-driven lookup
    # after the clone: resolve_previous_release_tag picks the highest release tag
    # strictly below <version> across all tags in the repo (reachable or not). It is
    # version-aware, works across a skipped minor (9.1.0 with no 9.0 line resolves
    # to the 8.2 line's last tag) and across a new major (9.0.0 -> the prior major's
    # last tag), and never trusts a guessed tag name. The resolution runs in
    # _run_cut once the tags are fetched; flag it here.
    resolve_rc1_baseline = stage == "rc1" and base_ref is None and not args.tag_glob

    # An explicit base_ref overrides tag resolution, so don't also derive a glob.
    # For an rc1 with resolve_rc1_baseline set, base_ref is still None here and the
    # glob stays None (rc1 has no same-version rc glob), so discovery uses the
    # previous-release baseline _run_cut resolves.
    tag_glob = None if base_ref else (args.tag_glob or _default_tag_glob(version, stage))
    # Whether the glob was *derived* (rc2+/ga default) rather than passed by the
    # maintainer. cut() rewrites a derived glob to the previous-release baseline for
    # a non-continuing first cut (a mis-dispatch, or a first GA of a new minor) so
    # it does not abort on an unreachable tag; an explicit --tag-glob is the
    # maintainer's intent and is left to resolve or fail loudly.
    tag_glob_derived = bool(tag_glob) and not args.tag_glob and not base_ref

    try:
        return _run_cut(
            token=args.token,
            repo_full_name=args.repo,
            source_ref=args.head_ref,
            version=version,
            stage=stage,
            urgency=urgency,
            date=args.date or None,
            tag_glob=tag_glob,
            tag_glob_derived=tag_glob_derived,
            base_ref=base_ref,
            contrib_base_ref=args.contrib_base_ref or None,
            security_fixes=args.security_fixes,
            security_from_advisories=args.security_from_advisories,
            dry_run=args.dry_run,
            force_ready=args.force_ready,
            resolve_rc1_baseline=resolve_rc1_baseline,
        )
    except subprocess.CalledProcessError as exc:  # surface git's stderr, not just the exit code
        # CalledProcessError.__str__ reports only the command and exit status;
        # git's actual message (auth failure, protected ref, rejected push) is in
        # stderr. Log it explicitly so an "exit 128" is diagnosable from CI logs.
        stderr = (exc.stderr or "").strip()
        logger.error(
            "Release cut failed: %s exited %s%s",
            " ".join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else exc.cmd,
            exc.returncode,
            f"\n{stderr}" if stderr else " (no stderr captured)",
        )
        return 1
    except ValueError as exc:
        # Validation errors (bad --base-ref, inconsistent branch state, malformed
        # version) carry a message written to be read on its own; log just that,
        # not a traceback, so it reads as cleanly as the CalledProcessError path.
        logger.error("Release cut failed: %s", exc)
        return 1
    except Exception:  # noqa: BLE001 - never crash the workflow uncaught
        logger.exception("Release cut failed")
        return 1


def _base_ref_exists(clone_dir: str, base_ref: str) -> bool:
    """True if *base_ref* resolves in the fresh clone, as itself or ``origin/<name>``.

    The candidate order (the ref as given, then ``origin/<name>``) must stay in
    step with :func:`discover._resolve_base_ref`, which resolves the range
    baseline the same way; if one gains a candidate (e.g. ``refs/tags/<name>``),
    the other must too, or validation here would pass a ref discovery then can't
    resolve (or vice versa).
    """
    for candidate in (base_ref, f"origin/{base_ref}"):
        try:
            run_git(clone_dir, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            return True
        except subprocess.CalledProcessError:
            continue
    return False


def _validate_base_ref(clone_dir: str, base_ref: str) -> None:
    """Raise a clear error if *base_ref* resolves to nothing in the fresh clone.

    Without this, a typo'd ``--base-ref`` is first hit deep in discovery and
    surfaces as an opaque exit-1 naming the ``origin/<name>`` form, not what the
    maintainer typed.
    """
    if not _base_ref_exists(clone_dir, base_ref):
        raise ValueError(
            f"--base-ref {base_ref!r} not found in the clone (tried {base_ref!r} and "
            f"'origin/{base_ref}'). Pass an existing branch, tag, or commit SHA."
        )


def _recredited_commit_count(
    clone_dir: str, base_ref: str, head_ref: str, prev_release_ref: str
) -> int | None:
    """How many commits in ``base_ref..head_ref`` are already reachable from the previous release.

    The discovery range is ``base_ref..head_ref``, which means "reachable from
    head, not from base". That is exactly the line's new history only when base
    is at/after the previous release on head's own history. If ``base_ref`` is
    too old (a typo landing on an older tag) or on a divergent branch, git walks
    back to the merge-base and the range re-includes commits the previous release
    already shipped, re-crediting old PRs into the notes and contributor list.

    We do not test ancestry of base against head directly: under valkey's
    fork-at-freeze model the correct base (the previous release tag) is not an
    ancestor of ``unstable`` (it sits on its own release branch), so an
    ``is_ancestor(base, head)`` guard would reject legitimate cuts. Instead we
    measure the actual damage: the count of range commits also reachable from
    *prev_release_ref*, which is 0 for any correct base (including the divergent
    previous-release tag) and positive only when the range reaches back past the
    previous release. Computed as ``|base..head| - |head ^base ^prev|``.

    Returns the re-credited count, or ``None`` if any ref does not resolve (the
    guard then does not fire; existence of base_ref is checked separately
    by :func:`_validate_base_ref`).
    """
    try:
        total = int(git_output(clone_dir, "rev-list", "--count", f"{base_ref}..{head_ref}").strip())
        new_only = int(
            git_output(
                clone_dir, "rev-list", "--count", head_ref, "--not", base_ref, prev_release_ref
            ).strip()
        )
    except subprocess.CalledProcessError:
        return None
    return max(0, total - new_only)


def _warn_if_base_ref_reaches_past_previous_release(
    clone_dir: str, base_ref: str, head_ref: str, version: str
) -> None:
    """Warn (do not block) when an explicit --base-ref widens the range past the previous release.

    An explicit ``--base-ref`` is validated only for existence (see
    :func:`_validate_base_ref`); nothing checks it is actually behind head on the
    line's own history. A too-old or divergent ref silently produces a range that
    re-credits already-released PRs (in the notes and the contributor list). This
    surfaces that as a loud warning so it is not silent, while still cutting:
    ``--base-ref`` is an intentional override and an unusual-but-valid base must
    remain possible.

    The check is skipped when the repo carries no release tag below *version*
    (the first release ever, or a tagless fork): there is no previous release to
    measure against, so there is nothing to compare and no warning to give.
    """
    resolved = discover_mod.resolve_previous_release_tag(clone_dir, version)
    if resolved is None:
        return
    prev_tag, _prev_sha = resolved
    recredited = _recredited_commit_count(clone_dir, base_ref, head_ref, prev_tag)
    if recredited:
        logger.warning(
            "--base-ref %r reaches back past the previous release %r: the range "
            "%s..%s re-includes %d commit(s) already shipped in %r, which will "
            "re-credit already-released PRs in the notes and contributor list. "
            "Cutting anyway (--base-ref is an explicit override); pass the previous "
            "release tag/branch if this is a mistake.",
            base_ref, prev_tag, base_ref, head_ref, recredited, prev_tag,
        )


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_iso_date(value: str) -> bool:
    """True if *value* is a valid ISO ``YYYY-MM-DD`` date.

    A malformed ``--date`` otherwise passes through ``release_format._format_date``
    into the dated heading (it returns unparseable input unchanged), so a typo
    like ``06/30/2026`` ships as the release date. Validate at the boundary; an
    empty value (defaulted to today) is handled before this.

    The format is checked explicitly rather than relying on
    ``date.fromisoformat``: on Python 3.11+ that function is lenient and accepts
    ``20260630`` and ISO week/ordinal forms like ``2026-W01-1`` (which silently
    resolves to a *different* calendar date), any of which would then ship into
    the heading. (Earlier supported versions down to the 3.9 floor are already
    strict, but we do not rely on that.) Requiring the ``YYYY-MM-DD`` shape first
    and parsing with ``strptime`` accepts only the documented form and still
    rejects impossible dates like ``2026-13-45``.
    """
    value = value.strip()
    if not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _run_cut(
    *,
    token: str,
    repo_full_name: str,
    source_ref: str,
    version: str,
    stage: str,
    urgency: str,
    date: str | None,
    tag_glob: str | None,
    base_ref: str | None,
    contrib_base_ref: str | None,
    security_fixes: list[str] | None,
    security_from_advisories: bool,
    dry_run: bool,
    force_ready: bool = False,
    resolve_rc1_baseline: bool = False,
    tag_glob_derived: bool = False,
) -> int:
    gh = Github(auth=Auth.Token(token))
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name), retries=3, description=f"get repo {repo_full_name}",
    )
    resolved_date = date or datetime.date.today().isoformat()
    baseline_unanchored = False
    with GitAuth(token, prefix="release-cut-git-askpass-") as auth:
        git_env = auth.env()
        clone_dir = tempfile.mkdtemp(prefix="release-cut-")
        try:
            run_git(None, "clone", "--branch", source_ref, github_https_url(repo_full_name),
                    clone_dir, env=git_env)
            run_git(clone_dir, "fetch", "--tags", "origin", env=git_env)
            if base_ref:
                # An explicit --base-ref that resolves to nothing aborts now, while
                # the error can still name what the maintainer typed (discovery
                # would later only see the origin/<name> form).
                _validate_base_ref(clone_dir, base_ref)
                # Existence is not enough: a too-old or divergent --base-ref widens
                # the range past the previous release and silently re-credits
                # already-shipped PRs. Discovery walks base_ref..source_ref for an
                # explicit base, so measure re-credited commits against that head
                # and warn (never block: --base-ref is an intentional override).
                _warn_if_base_ref_reaches_past_previous_release(
                    clone_dir, base_ref, source_ref, version
                )
            elif resolve_rc1_baseline:
                # rc1's true baseline is the previous release. Resolve it from the
                # tags actually in the repo (all tags, reachable or not): the
                # highest release tag strictly below this version. This spans a
                # skipped minor and a new major, and never trusts a guessed name.
                # When the repo carries no release below the target (the very first
                # release ever), there is nothing to anchor to: flag the baseline
                # unanchored and let the cut degrade to the full history (root..head)
                # so the PR body warns the range may be over-broad.
                resolved = discover_mod.resolve_previous_release_tag(clone_dir, version)
                if resolved is not None:
                    base_ref, _base_sha = resolved
                    logger.info(
                        "rc1 of %s: anchored discovery to the previous release tag %r.",
                        version, base_ref,
                    )
                else:
                    baseline_unanchored = True
                    logger.warning(
                        "rc1 of %s has no earlier release tag in the repo to anchor to "
                        "(first release ever, or a tagless fork). The cut will discover "
                        "over the full history to the head, which may span extra "
                        "history. Pass --base-ref explicitly (the previous release tag "
                        "or branch) to narrow it.",
                        version,
                    )
            return cut_mod.cut(
                repo,
                repo_full_name=repo_full_name,
                source_clone_dir=clone_dir,
                valkey_clone_dir=clone_dir,
                source_ref=source_ref,
                version=version, stage=stage, urgency=urgency, date=resolved_date,
                tag_glob=tag_glob, tag_glob_derived=tag_glob_derived,
                base_ref=base_ref, contrib_base_ref=contrib_base_ref,
                security_fixes=security_fixes, security_from_advisories=security_from_advisories,
                token=token, git_env=git_env, dry_run=dry_run,
                force_ready=force_ready,
                baseline_unanchored=baseline_unanchored,
            )
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
