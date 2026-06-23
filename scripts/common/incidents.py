"""Stable fingerprints for incident deduplication.

``compute_fingerprint(namespace, shapes)`` hashes an identity tuple plus
finding shapes. Volatile substrings (memory addresses, hex SHAs, node
ids, run-specific numbers) are normalized so two runs of the same
underlying failure produce the same fingerprint.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

_VOLATILE_RE = re.compile(
    # Order matters (left-to-right alternation): hex/SHA before bare digits,
    # and "node-N" before the bare \d+ so "node-5" collapses as one unit.
    r"0x[0-9a-f]+|\b[0-9a-f]{7,40}\b|\bnode[-_ ]?\d+\b|\b\d+\b",
    re.IGNORECASE,
)


def compute_fingerprint(*, namespace: Iterable[str], shapes: Iterable[str],
                        max_shapes: int = 8) -> str:
    """Stable hash grouping repeated failures by shape, not run ID.

    Normalization runs *before* dedup and slicing so volatile variants do
    not change which `max_shapes` survive the cap.

    ``namespace`` is the stable identity tuple (joined in order, never
    normalized); ``shapes`` are volatile finding details that are normalized,
    deduped, sorted, and capped. A caller whose entire identity is fixed (no
    volatile evidence) can pass it all in ``namespace`` with empty ``shapes``.
    """
    parts = [n.lower() for n in namespace]
    normalized = sorted({_VOLATILE_RE.sub("_", s.lower()) for s in shapes})[:max_shapes]
    parts.extend(normalized)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]
