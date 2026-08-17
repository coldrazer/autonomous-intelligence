from __future__ import annotations

import json
from pathlib import Path


SUPPORTED_CLIENTS = (
    "codex",
    "claude",
    "kimi",
    "antigravity",
    "gemini",
    "cursor",
    "vscode",
    "generic",
)


def render_client_config(
    client: str,
    workspace: str | Path,
    *,
    command: str = "autonomous-intelligence-mcp",
) -> str:
    """Render a host configuration without changing the host's existing files."""
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(f"unsupported MCP client: {client}")

    workspace_path = str(Path(workspace).resolve())
    args = ["--workspace", workspace_path]

    if client == "codex":
        command_value = json.dumps(command)
        args_value = json.dumps(args)
        return (
            "[mcp_servers.autonomous-intelligence]\n"
            f"command = {command_value}\n"
            f"args = {args_value}\n"
            "startup_timeout_sec = 20\n"
            "tool_timeout_sec = 120\n"
            'default_tools_approval_mode = "auto"\n\n'
            "[mcp_servers.autonomous-intelligence.tools.autonomous_write_file]\n"
            'approval_mode = "prompt"\n'
        )

    server: dict[str, object] = {"command": command, "args": args}
    if client == "claude":
        server["type"] = "stdio"
    elif client == "kimi":
        server.update({"startupTimeoutMs": 20_000, "toolTimeoutMs": 120_000})
    elif client == "gemini":
        server.update({"timeout": 120_000, "trust": False})
    elif client == "vscode":
        server["type"] = "stdio"

    root_key = "servers" if client == "vscode" else "mcpServers"
    return json.dumps(
        {root_key: {"autonomous-intelligence": server}},
        indent=2,
    ) + "\n"


def client_config_location(client: str) -> str:
    """Return the documented user-level configuration location for a client."""
    locations = {
        "codex": "~/.codex/config.toml",
        "claude": "~/.claude.json (or project .mcp.json)",
        "kimi": "~/.kimi/mcp.json or ~/.kimi-code/mcp.json",
        "antigravity": "~/.gemini/config/mcp_config.json",
        "gemini": "~/.gemini/settings.json",
        "cursor": "~/.cursor/mcp.json",
        "vscode": "MCP: Open User Configuration (or project .vscode/mcp.json)",
        "generic": "the client's MCP JSON configuration",
    }
    if client not in locations:
        raise ValueError(f"unsupported MCP client: {client}")
    return locations[client]
