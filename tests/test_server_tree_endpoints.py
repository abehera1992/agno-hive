"""Unit tests for the 3 new tree/branch/fork endpoint handlers (Phase 5/6).
Each handler function is called directly (not via a FastAPI TestClient --
this repo has no existing TestClient usage; mocking the swarm.sessions
calls it delegates to is consistent with how the rest of this file's
session endpoints work)."""
import pytest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException


@pytest.mark.asyncio
async def test_tree_endpoint_returns_messages():
    from api.server import get_session_tree_endpoint
    fake_tree = [{"id": 1, "parent_message_id": None, "role": "user", "content": "hi", "created_at": None, "depth": 0}]
    with patch("api.server.list_session_tree", new=AsyncMock(return_value=fake_tree)):
        result = await get_session_tree_endpoint("session-uuid")
    assert result["messages"] == fake_tree


@pytest.mark.asyncio
async def test_branch_endpoint_sets_leaf_to_parent_of_selected_message():
    from api.server import branch_session_endpoint
    from api.models import BranchRequest
    fake_tree = [
        {"id": 1, "parent_message_id": None, "role": "user", "content": "root", "created_at": None, "depth": 0},
        {"id": 2, "parent_message_id": 1, "role": "assistant", "content": "reply", "created_at": None, "depth": 1},
    ]
    with patch("api.server.list_session_tree", new=AsyncMock(return_value=fake_tree)), \
         patch("api.server.set_current_leaf", new=AsyncMock(return_value=True)) as mock_set:
        result = await branch_session_endpoint("session-uuid", BranchRequest(message_id=2))
    mock_set.assert_awaited_once_with("session-uuid", 1)  # rewinds to message 2's PARENT (id 1)
    assert result["new_leaf_id"] == 1
    assert result["editable_content"] == "reply"


@pytest.mark.asyncio
async def test_branch_endpoint_404s_on_unknown_message_id():
    from api.server import branch_session_endpoint
    from api.models import BranchRequest
    with patch("api.server.list_session_tree", new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await branch_session_endpoint("session-uuid", BranchRequest(message_id=999))
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_branch_endpoint_can_rewind_to_a_root_message_parent_none():
    """Branching from a root message (no parent) must rewind to leaf=None, not crash."""
    from api.server import branch_session_endpoint
    from api.models import BranchRequest
    fake_tree = [{"id": 1, "parent_message_id": None, "role": "user", "content": "root", "created_at": None, "depth": 0}]
    with patch("api.server.list_session_tree", new=AsyncMock(return_value=fake_tree)), \
         patch("api.server.set_current_leaf", new=AsyncMock(return_value=True)) as mock_set:
        result = await branch_session_endpoint("session-uuid", BranchRequest(message_id=1))
    mock_set.assert_awaited_once_with("session-uuid", None)
    assert result["new_leaf_id"] is None


@pytest.mark.asyncio
async def test_fork_endpoint_returns_new_session_id():
    from api.server import fork_session_endpoint
    from api.models import ForkRequest
    with patch("api.server.fork_session", new=AsyncMock(return_value="new-session-uuid")):
        result = await fork_session_endpoint("session-uuid", ForkRequest(title="forked task", project_id="ekam"))
    assert result["session_id"] == "new-session-uuid"


@pytest.mark.asyncio
async def test_fork_endpoint_404s_when_source_session_has_no_messages():
    from api.server import fork_session_endpoint
    from api.models import ForkRequest
    with patch("api.server.fork_session", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await fork_session_endpoint("session-uuid", ForkRequest(title="x", project_id="ekam"))
    assert exc_info.value.status_code == 404
