import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_mock_session(call_tool_side_effects: list):
    session = AsyncMock()
    session.call_tool.side_effect = call_tool_side_effects
    return session


def _text_result(*texts: str):
    return MagicMock(content=[MagicMock(text=t) for t in texts])


@pytest.mark.asyncio
async def test_load_from_session_discovers_patterns():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        call_tool_side_effects=[
            _text_result("patterns/backend.md\npatterns/frontend.md"),  # find_files
            _text_result("# Backend Rules"),                             # get_file_content backend
            _text_result("# Frontend Rules"),                            # get_file_content frontend
        ],
    )

    context = await _load_from_session(session, "patterns/**/*.md")

    assert "# Backend Rules" in context
    assert "# Frontend Rules" in context


@pytest.mark.asyncio
async def test_load_from_session_falls_back_to_project_context():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        call_tool_side_effects=[
            _text_result(""),               # find_files returns empty
            _text_result("# DOCS content"), # get_project_context fallback
        ],
    )

    context = await _load_from_session(session, "patterns/**/*.md")
    assert "# DOCS content" in context


@pytest.mark.asyncio
async def test_load_from_session_skips_failed_file_reads():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        call_tool_side_effects=[
            _text_result("patterns/good.md\npatterns/bad.md"),
            _text_result("# Good Content"),  # good.md succeeds
            Exception("read error"),          # bad.md fails — should be skipped
        ],
    )

    context = await _load_from_session(session, "patterns/**/*.md")
    assert "# Good Content" in context


@pytest.mark.asyncio
async def test_load_from_session_empty_when_no_content():
    from swarm.bootstrap import _load_from_session

    session = _make_mock_session(
        call_tool_side_effects=[
            _text_result(""),  # find_files empty
            _text_result(""),  # get_project_context empty
        ],
    )

    context = await _load_from_session(session, "patterns/**/*.md")
    assert context == ""
