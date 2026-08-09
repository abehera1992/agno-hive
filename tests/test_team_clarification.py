"""_extract_clarification: parses a ```needs_clarification fenced JSON block out of
the coordinator's final answer. See swarm/team.py's module comment above the
function for why this exists — a structured "I need a decision from the user"
signal, the same mechanism as Claude Code's own AskUserQuestion, carried over HTTP
via RunResponse.needs_clarification instead of in-process.
"""
import json

from swarm import team


def test_no_block_returns_content_unchanged_and_none():
    content = "Here is a normal, complete answer with no clarification needed."

    result, clarification = team._extract_clarification(content)

    assert result == content
    assert clarification is None


def test_valid_block_with_two_options_is_parsed_and_stripped():
    payload = {
        "question": "Which caching layer should this use?",
        "options": [
            {"label": "Redis", "description": "Shared across instances, needs infra"},
            {"label": "In-process LRU", "description": "Simpler, per-instance only"},
        ],
    }
    content = (
        "I looked at the caching request and found two valid approaches.\n\n"
        f"```needs_clarification\n{json.dumps(payload)}\n```"
    )

    result, clarification = team._extract_clarification(content)

    assert result == "I looked at the caching request and found two valid approaches."
    assert clarification == payload


def test_valid_block_with_four_options_is_parsed():
    payload = {
        "question": "Pick one",
        "options": [
            {"label": "A", "description": "a"},
            {"label": "B", "description": "b"},
            {"label": "C", "description": "c"},
            {"label": "D", "description": "d"},
        ],
    }
    content = f"```needs_clarification\n{json.dumps(payload)}\n```"

    result, clarification = team._extract_clarification(content)

    assert clarification is not None
    assert len(clarification["options"]) == 4


def test_option_without_description_defaults_to_none():
    payload = {
        "question": "Pick one",
        "options": [{"label": "A"}, {"label": "B"}],
    }
    content = f"```needs_clarification\n{json.dumps(payload)}\n```"

    result, clarification = team._extract_clarification(content)

    assert clarification["options"][0]["description"] is None


def test_single_option_is_malformed_stripped_but_no_clarification():
    payload = {"question": "Pick one", "options": [{"label": "Only one"}]}
    content = f"Some lead-in text.\n```needs_clarification\n{json.dumps(payload)}\n```"

    result, clarification = team._extract_clarification(content)

    assert clarification is None
    assert result == "Some lead-in text."


def test_five_options_is_malformed():
    payload = {
        "question": "Pick one",
        "options": [{"label": str(i)} for i in range(5)],
    }
    content = f"```needs_clarification\n{json.dumps(payload)}\n```"

    result, clarification = team._extract_clarification(content)

    assert clarification is None


def test_invalid_json_is_malformed_not_a_crash():
    content = "```needs_clarification\n{not valid json at all\n```"

    result, clarification = team._extract_clarification(content)

    assert clarification is None
    assert result == ""


def test_missing_options_key_is_malformed():
    content = '```needs_clarification\n{"question": "Pick one"}\n```'

    result, clarification = team._extract_clarification(content)

    assert clarification is None


def test_option_missing_label_is_malformed():
    payload = {"question": "Pick one", "options": [{"description": "no label here"}, {"label": "B"}]}
    content = f"```needs_clarification\n{json.dumps(payload)}\n```"

    result, clarification = team._extract_clarification(content)

    assert clarification is None


def test_empty_question_is_malformed():
    payload = {"question": "   ", "options": [{"label": "A"}, {"label": "B"}]}
    content = f"```needs_clarification\n{json.dumps(payload)}\n```"

    result, clarification = team._extract_clarification(content)

    assert clarification is None
