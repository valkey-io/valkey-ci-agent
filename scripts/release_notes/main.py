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
from scripts.common.proc import run_git
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

    ``git describe`` returns the tag with the shortest graph distance to HEAD
    regardless of release line, so a glob is needed to pin the baseline to the
    intended boundary. The boundary depends on the stage:

    * rc2+: the prior RC of this version, ``<version>-rc*`` (so a cut of
      9.1.0-rc3 walks back only to 9.1.0-rc2).
    * rc1 / ga / anything else: ``None``. rc1 has no prior same-version RC to
      anchor to (there is no rc0), and its true baseline is the previous
      release, which is not reachable from the source branch in valkey's
      fork-at-freeze model. So rc1 cannot resolve a tag automatically and must
      use ``--base-ref`` (see :func:`_default_base_ref_for_rc1`); ga continues an
      existing release line where the no-glob nearest tag is already correct.

    A version that is not ``M.m.p`` also returns None.
    """
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not m:
        return None
    rc = re.fullmatch(r"rc([1-9]\d*)", stage.strip().lower())
    if rc and int(rc.group(1)) >= 2:
        return f"{version.strip()}-rc*"
    return None


def _default_base_ref_for_rc1(version: str) -> str | None:
    """Best-effort previous-release baseline for an rc1 cut, e.g. 9.1.0 -> 9.0.0.

    The baseline depends on which component is being incremented, so the derived
    previous release differs by shape:

    * patch (``p > 0``, e.g. ``9.2.3``) -> the prior patch GA ``M.m.(p-1)``
      (``9.2.2``). A patch cut covers only the changes since the previous patch;
      deriving the previous *minor* (``9.1.0``) would re-credit the whole
      ``9.2.0``/``.1``/``.2`` patch history.
    * new minor (``p == 0``, ``m > 0``, e.g. ``9.2.0``) -> the previous minor's GA
      ``M.(m-1).0`` (``9.1.0``).
    * ``M.0.0`` -> None: the first release of a major has no previous release on
      this major to derive, and the prior major's final release is not derivable
      from the version alone; the user must supply ``--base-ref`` explicitly.

    We can only guess the tag's name; whether it is actually reachable as a range
    base is checked after the clone (see :func:`_validate_base_ref`), where a
    missing ref aborts with a clear error. Returns None when the version is not
    ``M.m.p`` or is ``M.0.0``.
    """
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not m:
        return None
    major, minor, patch = (int(g) for g in m.groups())
    if patch > 0:
        return f"{major}.{minor}.{patch - 1}"  # prior patch on the same line
    if minor > 0:
        return f"{major}.{minor - 1}.0"  # previous minor's GA
    return None  # M.0.0: first release of a major, no derivable previous release


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
    # no reachable release tag from the source branch, so tag resolution can't
    # find its baseline. rc1's true baseline is the previous release. If the user
    # did not pass --base-ref, warn loudly and default to the derived previous
    # release (M.(m-1).0); the cut still fails clearly at clone time if that ref
    # is absent, but most cuts get a sensible default instead of a hard error.
    # When no previous minor exists (M.0.0), there is nothing to derive: the cut
    # falls back to the nearest reachable tag, which may be over-broad, so flag it
    # in the PR body too (baseline_unanchored).
    baseline_unanchored = False
    base_ref_derived = False
    # An explicit --tag-glob means the user chose glob-based tag resolution, so
    # don't override it with the rc1 derived base (which would make base_ref
    # truthy and silently discard the glob below).
    if stage == "rc1" and base_ref is None and not args.tag_glob:
        derived = _default_base_ref_for_rc1(version)
        if derived:
            logger.warning(
                "rc1 of %s has no reachable baseline tag (there is no rc0, and release "
                "tags are not reachable from %r). Defaulting --base-ref to the previous "
                "release %r. Pass --base-ref explicitly to override (e.g. the previous "
                "release tag or its branch).",
                version, args.head_ref, derived,
            )
            base_ref = derived
            base_ref_derived = True
        else:
            baseline_unanchored = True
            logger.warning(
                "rc1 of %s has no reachable baseline tag and no previous-minor release "
                "could be derived. The cut will fall back to the nearest reachable tag, "
                "which may span a whole extra minor of history. Pass --base-ref explicitly "
                "(the previous release tag or branch) to anchor it.",
                version,
            )

    # An explicit (or rc1-defaulted) base_ref overrides tag resolution, so don't
    # also derive a glob.
    tag_glob = None if base_ref else (args.tag_glob or _default_tag_glob(version, stage))

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
            base_ref=base_ref,
            contrib_base_ref=args.contrib_base_ref or None,
            security_fixes=args.security_fixes,
            security_from_advisories=args.security_from_advisories,
            dry_run=args.dry_run,
            baseline_unanchored=baseline_unanchored,
            base_ref_derived=base_ref_derived,
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
    baseline_unanchored: bool = False,
    base_ref_derived: bool = False,
) -> int:
    gh = Github(auth=Auth.Token(token))
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name), retries=3, description=f"get repo {repo_full_name}",
    )
    resolved_date = date or datetime.date.today().isoformat()
    with GitAuth(token, prefix="release-cut-git-askpass-") as auth:
        git_env = auth.env()
        clone_dir = tempfile.mkdtemp(prefix="release-cut-")
        try:
            run_git(None, "clone", "--branch", source_ref, github_https_url(repo_full_name),
                    clone_dir, env=git_env)
            run_git(clone_dir, "fetch", "--tags", "origin", env=git_env)
            # Validate an explicit/derived baseline now, while the error can still
            # name what the maintainer typed (discovery would later only see the
            # origin/<name> form). An explicit --base-ref that is missing aborts.
            # A *derived* rc1 default that is missing (a tagless fork) instead
            # degrades to the nearest-tag fallback, matching the M.0.0 path, so a
            # guessed tag never hard-fails a cut the user did not ask to anchor.
            if base_ref and base_ref_derived and not _base_ref_exists(clone_dir, base_ref):
                logger.warning(
                    "Derived rc1 baseline %r is not present; falling back to the nearest "
                    "reachable tag. Pass --base-ref explicitly to anchor the range.",
                    base_ref,
                )
                base_ref = None
                baseline_unanchored = True
            elif base_ref:
                _validate_base_ref(clone_dir, base_ref)
            return cut_mod.cut(
                repo,
                repo_full_name=repo_full_name,
                source_clone_dir=clone_dir,
                valkey_clone_dir=clone_dir,
                source_ref=source_ref,
                version=version, stage=stage, urgency=urgency, date=resolved_date,
                tag_glob=tag_glob, base_ref=base_ref, contrib_base_ref=contrib_base_ref,
                security_fixes=security_fixes, security_from_advisories=security_from_advisories,
                token=token, git_env=git_env, dry_run=dry_run,
                baseline_unanchored=baseline_unanchored,
            )
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
