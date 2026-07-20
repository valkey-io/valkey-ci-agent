"""Entry point for the AI release-notes cut.

Clones valkey with full tags, discovers PRs in the range (HEAD back to the
previous tag), directly includes `release-notes` PRs, AI-triages the rest,
generates bullets via Claude/Bedrock, renders the dated section + version.h bump
+ contributor list, and opens a PR.
Returns 0 on success, 1 on failure, 2 on usage error.
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

_VALID_URGENCIES = ("LOW", "MODERATE", "HIGH", "CRITICAL", "SECURITY")

_DEFAULT_REPO = "valkey-io/valkey"


def _token() -> str:
    """Resolve the GitHub token from the environment variable chain."""
    return (
        os.environ.get("RELEASE_NOTES_GITHUB_TOKEN", "")
        or os.environ.get("TARGET_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
    )


def _env_flag(name: str) -> bool:
    """True if env var *name* holds a truthy string ('true'/'1'/'yes')."""
    return os.environ.get(name, "").strip().lower() in {"true", "1", "yes"}


def _default_tag_glob(version: str, stage: str) -> str | None:
    """Derive the baseline-tag match glob for this cut, or None.

    rc1 returns None (baseline resolved from previous-release tag).
    rc2+ returns ``<version>-rc*``. GA returns ``M.m.*``.
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


def _resolve_stage(version: str, stage: str) -> str:
    """Normalize an explicit stage, or infer GA for a patch release.

    A patch component greater than zero unambiguously identifies a normal patch
    release in Valkey's release model, so an omitted stage safely means ``ga``.
    ``M.m.0`` is shared by every RC and the initial GA; those releases must keep
    an explicit stage so dispatch cannot guess the maintainer's intent.
    """
    if stage.strip():
        return cut_mod._normalize_stage(stage)
    _major, _minor, patch = cut_mod._split_version(version)
    if patch > 0:
        return "ga"
    raise ValueError(
        "--stage is required for a .0 release; pass rc1..rcN or ga "
        "(patch versions infer ga when stage is omitted)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=_token(), help="GitHub token (App installation or PAT)")
    parser.add_argument("--repo", default=os.environ.get("RELEASE_NOTES_REPO", _DEFAULT_REPO),
                        help="Target repo, owner/name")
    parser.add_argument("--version", default=os.environ.get("RELEASE_NOTES_VERSION", ""),
                        help="Target version MAJOR.MINOR.PATCH, e.g. 9.1.0")
    parser.add_argument(
        "--stage",
        default=os.environ.get("RELEASE_NOTES_STAGE", ""),
        help="Release stage: rc1..rcN or ga. Optional for patch versions, which infer ga.",
    )
    parser.add_argument("--urgency", default=os.environ.get("RELEASE_NOTES_URGENCY", ""),
                        help="Upgrade urgency: LOW, MODERATE, HIGH, CRITICAL, SECURITY")
    parser.add_argument("--date", default=os.environ.get("RELEASE_NOTES_DATE", ""),
                        help="Release date YYYY-MM-DD (default: current UTC date)")
    parser.add_argument("--tag-glob", default=os.environ.get("RELEASE_NOTES_TAG_GLOB", ""),
                        help="Optional --match glob restricting the baseline tag, e.g. '9.1.0-rc*'")
    parser.add_argument("--base-ref", default=os.environ.get("RELEASE_NOTES_BASE_REF", ""),
                        help="Explicit baseline ref (branch/tag/SHA) overriding tag resolution. "
                             "Leave empty for normal cuts; tags resolve the previous release, "
                             "RC, or patch automatically. Set only to correct a range that the "
                             "dry-run preview proves is wrong.")
    parser.add_argument("--contrib-base-ref", default=os.environ.get("RELEASE_NOTES_CONTRIB_BASE", ""),
                        help="Contributor range start (default: notes baseline, then tag/root)")
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
    if not (args.version and args.urgency):
        parser.error("--version and --urgency are required")

    # Validate and canonicalize inputs before the expensive clone + AI run.
    try:
        version = cut_mod.canonical_version(args.version)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        stage = _resolve_stage(version, args.stage)
    except ValueError as exc:
        parser.error(str(exc))
    urgency = args.urgency.strip().upper()
    if urgency not in _VALID_URGENCIES:
        parser.error(f"--urgency must be one of {', '.join(_VALID_URGENCIES)}, got {args.urgency!r}")
    if args.date and not _is_iso_date(args.date):
        parser.error(f"--date must be ISO YYYY-MM-DD (e.g. 2026-06-30), got {args.date!r}")

    base_ref = args.base_ref or None

    # rc1 baseline is resolved from the repo's tags after clone (no same-version RC to anchor to).
    resolve_rc1_baseline = stage == "rc1" and base_ref is None and not args.tag_glob

    tag_glob = None if base_ref else (args.tag_glob or _default_tag_glob(version, stage))

    try:
        return _run_cut(
            token=args.token,
            repo_full_name=args.repo,
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
            force_ready=args.force_ready,
            resolve_rc1_baseline=resolve_rc1_baseline,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        logger.error(
            "Release cut failed: %s exited %s%s",
            " ".join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else exc.cmd,
            exc.returncode,
            f"\n{stderr}" if stderr else " (no stderr captured)",
        )
        return 1
    except ValueError as exc:
        logger.error("Release cut failed: %s", exc)
        return 1
    except Exception:  # noqa: BLE001 - never crash the workflow uncaught
        logger.exception("Release cut failed")
        return 1


def _base_ref_exists(clone_dir: str, base_ref: str) -> bool:
    """True if *base_ref* resolves in the clone (tries bare name then origin/<name>)."""
    for candidate in (base_ref, f"origin/{base_ref}"):
        try:
            run_git(clone_dir, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            return True
        except subprocess.CalledProcessError:
            continue
    return False


def _validate_base_ref(clone_dir: str, base_ref: str) -> None:
    """Raise ValueError if *base_ref* does not resolve in the clone."""
    if not _base_ref_exists(clone_dir, base_ref):
        raise ValueError(
            f"--base-ref {base_ref!r} not found in the clone (tried {base_ref!r} and "
            f"'origin/{base_ref}'). Pass an existing branch, tag, or commit SHA."
        )


def _recredited_commit_count(
    clone_dir: str, base_ref: str, head_ref: str, prev_release_ref: str
) -> int | None:
    """Count commits in base_ref..head_ref already reachable from prev_release_ref.

    Returns 0 for a correct base, positive when the range extends past the
    previous release, or None if any ref fails to resolve.
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
    """Warn (not block) when --base-ref widens the range past the previous release."""
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
    """True if *value* is a valid YYYY-MM-DD date string."""
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
) -> int:
    gh = Github(auth=Auth.Token(token))
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name), retries=3, description=f"get repo {repo_full_name}",
    )
    resolved_date = date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    baseline_unanchored = False
    with GitAuth(token, prefix="release-cut-git-askpass-") as auth:
        git_env = auth.env()
        clone_dir = tempfile.mkdtemp(prefix="release-cut-")
        try:
            # Derive the M.m branch from the version (all stages target it).
            major, minor, _patch = cut_mod._split_version(version)
            source_ref = f"{major}.{minor}"
            run_git(None, "clone", "--branch", source_ref, github_https_url(repo_full_name),
                    clone_dir, env=git_env)
            run_git(clone_dir, "fetch", "--tags", "origin", env=git_env)
            _validate_release_target(clone_dir, source_ref, version, stage)
            if base_ref:
                _validate_base_ref(clone_dir, base_ref)
                _warn_if_base_ref_reaches_past_previous_release(
                    clone_dir, base_ref, source_ref, version
                )
            elif resolve_rc1_baseline:
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
                version=version, stage=stage, urgency=urgency, date=resolved_date,
                tag_glob=tag_glob,
                base_ref=base_ref, contrib_base_ref=contrib_base_ref,
                security_fixes=security_fixes, security_from_advisories=security_from_advisories,
                token=token, git_env=git_env, dry_run=dry_run,
                force_ready=force_ready,
                baseline_unanchored=baseline_unanchored,
                github=gh,
            )
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)


def _validate_release_target(
    clone_dir: str, source_ref: str, version: str, stage: str
) -> None:
    """Fail before AI work if the requested release does not advance the line."""
    version_h = Path(clone_dir, cut_mod.VERSION_FILE).read_text(encoding="utf-8")
    cut_mod.validate_release_progression(version_h, version, stage)
    discover_mod.validate_target_release_tag(
        clone_dir, source_ref, version, stage
    )


if __name__ == "__main__":
    raise SystemExit(main())
