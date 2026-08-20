"""Targets contract for the CVE build-and-verify workflow.

The scan emits the set of fixable (image, line, variant, platform, cve,
package, fixed_version) tuples as a base64 JSON blob. The workflow agent codes
against this exact shape, so it is PINNED: decode is strict and fails loudly on
any malformed input rather than silently returning an empty list (which would
let a broken contract pass artifact verification with nothing to check).

Deterministic, stdlib only, no I/O.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from dataclasses import dataclass

#: The exact keys every encoded target dict carries, in contract order.
_TARGET_KEYS = (
    "image",
    "line",
    "variant",
    "platform",
    "cve",
    "package",
    "fixed_version",
)


@dataclass(frozen=True)
class Target:
    """One fixable (image, platform, cve, package) verification target.

    ``line``/``variant`` identify the valkey-container build (e.g. line "8.0",
    variant "alpine"); ``fixed_version`` is the published fix the rebuilt
    artifact must reach or exceed.
    """

    image: str
    line: str
    variant: str
    platform: str
    cve: str
    package: str
    fixed_version: str


class TargetDecodeError(Exception):
    """Raised when a base64 targets contract is malformed."""


def line_variant_from_image(image: str) -> tuple[str, str]:
    """Derive (line, variant) from a valkey image ref.

    ``valkey/valkey:8.0`` -> ``("8.0", "debian")``;
    ``valkey/valkey:8.0-alpine`` -> ``("8.0", "alpine")``. Strips the
    ``-alpine`` SUFFIX only, never any other occurrence of the substring.
    """
    tag = image.rsplit(":", 1)[-1] if ":" in image else image
    if tag.endswith("-alpine"):
        return tag[: -len("-alpine")], "alpine"
    return tag, "debian"


def encode_targets(targets: list[Target]) -> str:
    """Encode targets as base64 of a compact JSON list of dicts.

    Each dict carries exactly ``_TARGET_KEYS``. Compact separators keep the
    blob small enough for a GitHub Actions job output.
    """
    payload = [
        {
            "image": t.image,
            "line": t.line,
            "variant": t.variant,
            "platform": t.platform,
            "cve": t.cve,
            "package": t.package,
            "fixed_version": t.fixed_version,
        }
        for t in targets
    ]
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def decode_targets(raw: str) -> list[Target]:
    """Decode a base64 targets contract into Target objects, strictly.

    Raises TargetDecodeError on bad base64, non-UTF-8 or non-JSON content, a
    non-list payload, a non-dict entry, a key set that is not EXACTLY the
    contract keys (missing or extra), or any non-string value. Never silently
    returns [] for malformed input: an empty list decodes only from the
    encoding of a genuinely empty list.
    """
    if not isinstance(raw, str):
        raise TargetDecodeError(
            f"targets must be a base64 string, got {type(raw).__name__}"
        )

    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TargetDecodeError(f"targets is not valid base64: {exc}") from exc

    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetDecodeError(f"targets is not valid JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise TargetDecodeError(
            f"targets payload must be a JSON list, got {type(payload).__name__}"
        )

    expected = set(_TARGET_KEYS)
    targets: list[Target] = []
    for idx, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise TargetDecodeError(
                f"targets[{idx}] must be an object, got {type(entry).__name__}"
            )
        keys = set(entry)
        if keys != expected:
            missing = sorted(expected - keys)
            extra = sorted(keys - expected)
            raise TargetDecodeError(
                f"targets[{idx}] key mismatch: missing={missing}, extra={extra}"
            )
        for key in _TARGET_KEYS:
            value = entry[key]
            if not isinstance(value, str):
                raise TargetDecodeError(
                    f"targets[{idx}].{key} must be a string, got "
                    f"{type(value).__name__}"
                )
        targets.append(Target(**{key: entry[key] for key in _TARGET_KEYS}))
    return targets


def verify_matrix(targets: list[Target]) -> list[dict]:
    """Dedupe targets into verification build jobs, one per affected arch.

    Policy: emit one entry per DISTINCT (line, variant, platform) present in
    the targets, deduplicated and order-stable. We verify EVERY affected
    architecture because a distro can publish a package fix for one arch before
    another: verifying only amd64 would pass a line whose fix is live on amd64
    but not yet on arm64, dispatch the rebuild, and leave the rebuilt arm64
    image vulnerable while the summary claims the line was fixed. One build per
    affected arch removes that overstatement. Each entry is
    ``{line, variant, platform, image}``.

    Input order is preserved: entries appear in first-seen (line, variant,
    platform) order; the image is the first one seen for its (line, variant).
    """
    image_of: dict[tuple[str, str], str] = {}
    seen: set[tuple[str, str, str]] = set()
    entries: list[dict] = []
    for t in targets:
        lv = (t.line, t.variant)
        # First image seen for a (line, variant) is the one we build for it.
        image_of.setdefault(lv, t.image)
        key = (t.line, t.variant, t.platform)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "line": t.line,
                "variant": t.variant,
                "platform": t.platform,
                "image": image_of[lv],
            }
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    """CLI: turn a base64 targets contract into the verify-matrix include list.

    ``--verify-matrix <base64>`` prints ``verify_matrix(...)`` as a single-line
    JSON array on stdout, ready for a GitHub Actions ``strategy.matrix.include``
    via ``fromJSON``. A malformed contract exits nonzero (fail closed) so a
    broken blob never yields a silently empty matrix.
    """
    parser = argparse.ArgumentParser(
        description="Targets contract utilities for the CVE verify matrix.",
    )
    parser.add_argument(
        "--verify-matrix",
        metavar="BASE64",
        required=True,
        help="Base64 targets contract; prints the verify-matrix include list as JSON.",
    )
    args = parser.parse_args(argv)

    try:
        targets = decode_targets(args.verify_matrix)
    except TargetDecodeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(verify_matrix(targets)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
