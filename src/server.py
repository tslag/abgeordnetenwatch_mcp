import os

from fastmcp import FastMCP

from prompts.workflows import register_prompts
from resources.prompts import INSTRUCTION_PROMPT
from resources.resources import register_resources
from tools.lookup import register_lookup_tools
from tools.politicians import register_politician_tools
from tools.polls import register_poll_tools
from tools.programs import register_program_tools
from tools.raw import register_raw_tools
from tools.sidejobs import register_sidejob_tools
from tools.votes import register_vote_tools


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="abgeordnetenwatch-mcp",
        version="1.0.0",
        instructions=INSTRUCTION_PROMPT,
    )
    register_resources(mcp)
    register_prompts(mcp)
    register_lookup_tools(mcp)
    register_politician_tools(mcp)
    register_poll_tools(mcp)
    register_vote_tools(mcp)
    register_sidejob_tools(mcp)
    register_program_tools(mcp)
    if os.environ.get("AW_ENABLE_RAW_QUERY", "").lower() in ("1", "true", "yes"):
        register_raw_tools(mcp)
    return mcp


aw_mcp = build_server()


if __name__ == "__main__":
    aw_mcp.run()
