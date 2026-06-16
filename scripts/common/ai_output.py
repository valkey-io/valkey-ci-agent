"""Parse a JSON object out of a Claude Code subprocess's output.

Claude Code emits stream-json: a sequence of event lines ending in a
``result`` event whose ``result`` field holds the model's final text. The
model is asked to return a single JSON object; this finds it whether it
arrives wrapped in the stream-json ``result`` event or as bare output.

Shared by every workflow that asks Claude for a structured verdict.
"""

from __future__ import annotations

import json
from typing import Any


def extract_json_object(stdout: str, *, required_key: str) -> dict[str, Any] | None:
    """Return the first ``{...}`` object containing ``required_key``, or None.

    Prefers the last stream-json ``result`` event's text, then scans for a
    JSON object carrying ``required_key`` so we ignore unrelated braces in
    surrounding prose.
    """
    text = stdout
    for line in stdout.strip().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event.get("result")
            if isinstance(result, str):
                text = result

    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict) and required_key in obj:
            return obj
        start = text.find("{", start + 1)
    return None
