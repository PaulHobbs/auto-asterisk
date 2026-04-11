"""Unit tests for llm.extract_json and llm.extract_float."""

import pytest
from auto.llm import extract_json, extract_float


class TestExtractJson:
    def test_raw_json_object(self):
        assert extract_json('{"key": "value"}') == {"key": "value"}

    def test_fenced_json(self):
        text = "Here's the result:\n```json\n{\"score\": 42}\n```\nDone."
        assert extract_json(text) == {"score": 42}

    def test_fenced_without_lang(self):
        text = "```\n{\"a\": 1}\n```"
        assert extract_json(text) == {"a": 1}

    def test_json_array(self):
        text = 'Results: [{"title": "A"}, {"title": "B"}]'
        result = extract_json(text)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_json_embedded_in_prose(self):
        text = 'The analysis shows that {"score": 0.5, "status": "ok"} is the result.'
        assert extract_json(text) == {"score": 0.5, "status": "ok"}

    def test_nested_json(self):
        text = '{"outer": {"inner": [1, 2, 3]}}'
        result = extract_json(text)
        assert result["outer"]["inner"] == [1, 2, 3]

    def test_no_json(self):
        assert extract_json("No JSON here at all.") is None

    def test_empty_string(self):
        assert extract_json("") is None

    def test_malformed_json(self):
        # Incomplete JSON should not crash
        result = extract_json("{bad json here")
        # Either None or manages to parse something
        assert result is None or isinstance(result, (dict, list))

    def test_json_with_escaped_quotes(self):
        text = '{"message": "He said \\"hello\\""}'
        result = extract_json(text)
        assert result is not None
        assert "hello" in result["message"]

    def test_multiple_json_blocks_returns_first(self):
        text = '{"first": 1} and then {"second": 2}'
        result = extract_json(text)
        assert result == {"first": 1}

    def test_array_before_object(self):
        text = '[{"id": 1}] then {"other": true}'
        result = extract_json(text)
        assert isinstance(result, list)

    def test_json_with_braces_in_string(self):
        text = '{"code": "if (x) { y(); }"}'
        result = extract_json(text)
        assert result == {"code": "if (x) { y(); }"}

    def test_extract_json_with_newlines_in_value(self):
        text = '{"msg": "line1\\nline2"}'
        result = extract_json(text)
        assert result["msg"] == "line1\nline2"


class TestExtractFloat:
    def test_json_score(self):
        assert extract_float('{"score": 3.14}') == pytest.approx(3.14)

    def test_score_colon_format(self):
        assert extract_float("Score: 42.5") == pytest.approx(42.5)

    def test_score_equals_format(self):
        assert extract_float("score=99.1") == pytest.approx(99.1)

    def test_standalone_number(self):
        assert extract_float("The answer is 7.0") is None

    def test_rejects_line_numbers(self):
        assert extract_float("Line 42: error occurred") is None

    def test_rejects_timestamps(self):
        assert extract_float("completed in 3.5 seconds") is None

    def test_no_number(self):
        assert extract_float("no numbers here") is None

    def test_integer_score(self):
        assert extract_float('{"score": 0}') == pytest.approx(0.0)

    def test_fenced_json_score(self):
        text = "```json\n{\"score\": 15.5}\n```"
        assert extract_float(text) == pytest.approx(15.5)
