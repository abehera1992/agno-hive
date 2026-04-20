from agno.team import Team
from agno.tools.mcp import MCPTools

from .agents import make_coder, make_leader, make_reviewer
from config.config import config


def build_swarm() -> Team:
    """Assemble the AgnoHive swarm. Call once per process."""
    mcp = MCPTools(url=config.mcp_url, transport="sse")

    return Team(
        name="AgnoHive",
        mode="collaborate",
        leader=make_leader(mcp),
        members=[make_coder(mcp), make_reviewer(mcp)],
        share_context=True,
        show_members_responses=True,
    )
