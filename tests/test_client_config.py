from __future__ import annotations

import json
from pathlib import Path

import pytest

from autonomous_intelligence.client_config import SUPPORTED_CLIENTS, render_client_config


@pytest.mark.parametrize(
    "client",
    ["claude", "kimi", "antigravity", "gemini", "cursor", "generic"],
)
def test_json_mcp_client_configs_are_valid(client: str, tmp_path: Path):
    rendered = render_client_config(client, tmp_path, command="ai-mcp")
    payload = json.loads(rendered)
    server = payload["mcpServers"]["autonomous-intelligence"]
    assert server["command"] == "ai-mcp"
    assert server["args"] == ["--workspace", str(tmp_path.resolve())]


def test_vscode_uses_its_servers_schema(tmp_path: Path):
    payload = json.loads(render_client_config("vscode", tmp_path))
    server = payload["servers"]["autonomous-intelligence"]
    assert server["type"] == "stdio"


def test_codex_config_preserves_write_approval(tmp_path: Path):
    rendered = render_client_config("codex", tmp_path, command="ai-mcp")
    assert "[mcp_servers.autonomous-intelligence]" in rendered
    assert 'command = "ai-mcp"' in rendered
    assert "autonomous_write_file" in rendered
    assert 'approval_mode = "prompt"' in rendered


def test_every_supported_client_renders(tmp_path: Path):
    assert all(render_client_config(client, tmp_path) for client in SUPPORTED_CLIENTS)
