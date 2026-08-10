"""request_clarification / _extract_clarification_from_tools: the primary
clarification-request mechanism since 2026-08-10, replacing the original
```needs_clarification fenced-text convention (which stays as a fallback --
see test_team_clarification.py and swarm/team.py's module comment above
_CLARIFICATION_RE for why the fenced-text approach failed intermittently).

request_clarification is a real tool (stop_after_tool_call=True) on the
coordinator's own tool list -- calling it is a first-class action the model
already reliably decides on via normal tool-calling, not a text formatting
convention layered on top of free-text generation. _extract_clarification_from_tools
reads the tool call's own (already-validated) arguments off a completed run's
`.tools` list instead of regex-parsing prose.
"""
from types import SimpleNamespace

from swarm import team


def _fake_result(tools):
    """A minimal stand-in for TeamRunOutput / a stream's last event -- both
    expose `.tools: list[ToolExecution]`, duck-typed via getattr in
    _extract_clarification_from_tools."""
    return SimpleNamespace(tools=tools)


def _fake_tool_execution(tool_name, tool_args):
    return SimpleNamespace(tool_name=tool_name, tool_args=tool_args)


# ── request_clarification: builds as a real Function with the right shape ──────

def test_request_clarification_is_a_function_named_correctly():
    assert team.request_clarification.name == "request_clarification"


def test_request_clarification_stops_the_run_after_being_called():
    assert team.request_clarification.stop_after_tool_call is True


def test_request_clarification_parameters_require_question_and_options():
    params = team.request_clarification.parameters
    assert set(params["required"]) == {"question", "options"}


# ── _build_team: request_clarification is always on the coordinator's tools ────

def test_build_team_includes_request_clarification_in_coordinator_tools(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = team._build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
    )

    assert team.request_clarification in result.tools


def test_build_team_includes_request_clarification_even_when_read_only(monkeypatch):
    monkeypatch.setattr("swarm.team.config.inference_backend", "ollama")

    result = team._build_team(
        agent_specs=None,
        coordinator_model="qwen2.5-coder:32b",
        coordinator_tools=None,
        mode="coordinate",
        mcp_list=[],
        instructions=[],
        read_only=True,
    )

    assert team.request_clarification in result.tools


# ── _extract_clarification_from_tools: no matching tool call ───────────────────

def test_no_tools_returns_none():
    assert team._extract_clarification_from_tools(_fake_result(tools=None)) is None
    assert team._extract_clarification_from_tools(_fake_result(tools=[])) is None


def test_unrelated_tool_calls_return_none():
    tools = [
        _fake_tool_execution("get_file_content", {"relative_path": "foo.py"}),
        _fake_tool_execution("search_files", {"pattern": "x"}),
    ]
    assert team._extract_clarification_from_tools(_fake_result(tools=tools)) is None


# ── _extract_clarification_from_tools: valid call ───────────────────────────────

def test_valid_call_is_extracted():
    tools = [
        _fake_tool_execution(
            "request_clarification",
            {
                "question": "Which caching layer should this use?",
                "options": [
                    {"label": "Redis", "description": "Shared across instances, needs infra"},
                    {"label": "In-process LRU", "description": "Simpler, per-instance only"},
                ],
            },
        )
    ]

    result = team._extract_clarification_from_tools(_fake_result(tools=tools))

    assert result == {
        "question": "Which caching layer should this use?",
        "options": [
            {"label": "Redis", "description": "Shared across instances, needs infra"},
            {"label": "In-process LRU", "description": "Simpler, per-instance only"},
        ],
    }


def test_valid_call_among_other_tool_calls_is_found():
    tools = [
        _fake_tool_execution("get_file_content", {"relative_path": "foo.py"}),
        _fake_tool_execution(
            "request_clarification",
            {"question": "Proceed how?", "options": [{"label": "A"}, {"label": "B"}]},
        ),
    ]

    result = team._extract_clarification_from_tools(_fake_result(tools=tools))

    assert result["question"] == "Proceed how?"


def test_option_missing_description_defaults_to_none():
    tools = [
        _fake_tool_execution(
            "request_clarification",
            {"question": "Proceed how?", "options": [{"label": "A"}, {"label": "B"}]},
        )
    ]

    result = team._extract_clarification_from_tools(_fake_result(tools=tools))

    assert result["options"] == [{"label": "A", "description": None}, {"label": "B", "description": None}]


# ── _extract_clarification_from_tools: malformed calls degrade to None ─────────

def test_empty_question_is_ignored():
    tools = [
        _fake_tool_execution(
            "request_clarification",
            {"question": "  ", "options": [{"label": "A"}, {"label": "B"}]},
        )
    ]
    assert team._extract_clarification_from_tools(_fake_result(tools=tools)) is None


def test_single_option_is_ignored():
    tools = [
        _fake_tool_execution(
            "request_clarification",
            {"question": "Proceed how?", "options": [{"label": "A"}]},
        )
    ]
    assert team._extract_clarification_from_tools(_fake_result(tools=tools)) is None


def test_five_options_is_ignored():
    tools = [
        _fake_tool_execution(
            "request_clarification",
            {
                "question": "Proceed how?",
                "options": [{"label": str(i)} for i in range(5)],
            },
        )
    ]
    assert team._extract_clarification_from_tools(_fake_result(tools=tools)) is None


def test_option_missing_label_is_ignored():
    tools = [
        _fake_tool_execution(
            "request_clarification",
            {"question": "Proceed how?", "options": [{"description": "no label"}, {"label": "B"}]},
        )
    ]
    assert team._extract_clarification_from_tools(_fake_result(tools=tools)) is None


def test_missing_tool_args_is_ignored():
    tools = [_fake_tool_execution("request_clarification", None)]
    assert team._extract_clarification_from_tools(_fake_result(tools=tools)) is None


def test_options_not_a_list_is_ignored():
    tools = [
        _fake_tool_execution(
            "request_clarification",
            {"question": "Proceed how?", "options": "not-a-list"},
        )
    ]
    assert team._extract_clarification_from_tools(_fake_result(tools=tools)) is None
