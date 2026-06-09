"""MCP tool seed hook.

Default MCP tools are intentionally disabled for now because the previous seeded
commands were placeholders and depended on external CLIs that are not bundled
with lmWorld.
"""


DEFAULT_TOOLS: list[dict] = []


async def seed_tools():
    """Keep the startup hook, but do not seed any MCP tools by default."""
    return None
