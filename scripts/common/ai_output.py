"""Parse a JSON object out of a Claude Code subprocess's output.

Claude Code emits stream-json: a sequence of event lines ending in a
``result`` event whose ``result`` field holds the model's final text. The
model is asked to return a single JSON object; this finds it whether it
arrives wrapped in the stream-json ``result`` event or as bare output.

Shared by every workflow that asks Claude for a structured verdict.
"""

from __future__ import annotations

import json
from typing import Any, Callable


def extract_json_object(stdout: str, *, required_key: str, strict: bool = False) -> dict[str, Any] | None:
    """Return the first ``{...}`` object containing ``required_key``, or None.

    Prefers the last stream-json ``result`` event's text, then scans for a
    JSON object carrying ``required_key`` so we ignore unrelated braces in
    surrounding prose.

    ``strict`` refuses ambiguous output instead (see ``_extract_one``), for a
    caller whose verdict gates a change.
    """
    if strict:
        return _extract_one(stdout, required_key=required_key)
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


def _extract_one(stdout: str, *, required_key: str) -> dict[str, Any] | None:
    """Return the one ``{...}`` object containing ``required_key``, or None.

    Reads the stream-json ``result`` event's text when there is one, then scans
    it for top-level JSON objects carrying ``required_key`` so unrelated braces
    in surrounding prose are ignored. Output that is ambiguous returns None
    rather than a guess: more than one ``result`` event, two different objects
    carrying the key (an example verdict followed by the real one), or an
    object with a duplicated key.
    """
    text = stdout
    results = 0
    for line in stdout.strip().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            results += 1
            result = event.get("result")
            if isinstance(result, str):
                text = result
    if results > 1:
        return None

    decoder = json.JSONDecoder(object_pairs_hook=_unique_keys_in_verdict(required_key))
    found: list[dict[str, Any]] = []
    start = text.find("{")
    while start != -1:
        try:
            obj, length = decoder.raw_decode(text[start:])
        except _DuplicateKey:
            return None
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict) and required_key in obj:
            found.append(obj)
            # Skip the object's body so its nested objects are not candidates.
            start = text.find("{", start + length)
        else:
            start = text.find("{", start + 1)
    if not found or any(obj != found[0] for obj in found[1:]):
        return None
    return found[0]


class _DuplicateKey(ValueError):
    pass


def _unique_keys_in_verdict(required_key: str) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
    """A decoder hook that refuses a duplicated key in an object carrying ``required_key``.

    Only there: a duplicated key in some other object the prose mentions
    says nothing about the verdict.
    """
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        if required_key in keys and len(set(keys)) != len(keys):
            raise _DuplicateKey(f"duplicate key in {keys}")
        return dict(pairs)
    return hook
