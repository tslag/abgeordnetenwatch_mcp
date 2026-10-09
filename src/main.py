"""Entry point: start the Abgeordnetenwatch MCP server on stdio.

    uv run python src/main.py
"""
from server import aw_mcp


def main() -> None:
    aw_mcp.run()


if __name__ == "__main__":
    main()
