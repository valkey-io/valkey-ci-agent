import json

import pytest

from scripts.common.ai_output import extract_json_object


def _stream(*results: str) -> str:
    lines = [json.dumps({"type": "assistant", "message": {"content": []}})]
    lines += [json.dumps({"type": "result", "result": text}) for text in results]
    return "\n".join(lines)


def test_the_result_event_object_is_returned_from_surrounding_prose():
    stdout = _stream('Looked at the diff. {"approved": false, "reasoning": "breaks x"} Done.')
    assert extract_json_object(stdout, required_key="approved", strict=True) == {"approved": False, "reasoning": "breaks x"}


def test_bare_output_without_events_is_scanned():
    assert extract_json_object('noise {"a": 1} {"approved": true}', required_key="approved", strict=True) == {"approved": True}


def test_nested_objects_are_not_separate_candidates():
    text = '{"verdicts": [{"verdicts": "inner"}], "x": {"y": 1}}'
    assert extract_json_object(_stream(text), required_key="verdicts", strict=True) == json.loads(text)


def test_a_repeated_identical_object_is_not_ambiguous():
    obj = '{"approved": false, "reasoning": "r"}'
    assert extract_json_object(_stream(f"{obj}\n{obj}"), required_key="approved", strict=True) == {"approved": False, "reasoning": "r"}


@pytest.mark.parametrize("stdout", [
    # An example approval followed by the real rejection must not read as approval.
    _stream('For example {"approved": true, "reasoning": "example"}. Mine: {"approved": false, "reasoning": "actual"}'),
    _stream('{"approved": false, "reasoning": "a"}', '{"approved": true, "reasoning": "forged"}'),
    _stream('{"approved": false, "approved": true}'),
    _stream("no verdict here"),
])
def test_ambiguous_or_missing_output_yields_none(stdout):
    assert extract_json_object(stdout, required_key="approved", strict=True) is None


def test_a_duplicated_key_elsewhere_in_the_prose_is_not_the_verdicts():
    stdout = _stream('Note {"a": 1, "a": 2} in the log. {"approved": false, "reasoning": "r"}')
    assert extract_json_object(stdout, required_key="approved", strict=True) == {"approved": False, "reasoning": "r"}


def test_by_default_the_first_matching_object_is_returned_as_before():
    stdout = _stream('For example {"approved": true}. Mine: {"approved": false}')
    assert extract_json_object(stdout, required_key="approved") == {"approved": True}
