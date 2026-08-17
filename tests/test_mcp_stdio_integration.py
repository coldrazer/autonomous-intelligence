from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from autonomous_intelligence.ipc import RemoteBrokerClient, default_pipe_name, load_or_create_ipc_key


@pytest.mark.skipif(os.name != "nt", reason="full named-pipe integration is Windows-specific")
def test_stdio_mcp_to_named_pipe_broker_to_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    (workspace / "source.txt").write_text("stdio chain", encoding="utf-8")
    address = default_pipe_name(workspace)
    key = load_or_create_ipc_key(state / "ipc.key")
    broker_client = RemoteBrokerClient(address, key)
    broker_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "autonomous_intelligence.cli",
            "--workspace",
            str(workspace),
            "--state-dir",
            str(state),
            "broker",
            "--approval-mode",
            "allow",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                broker_client.status(str(uuid.uuid4()))
                break
            except OSError:
                if time.monotonic() >= deadline:
                    stderr = broker_process.stderr.read().decode(errors="replace")
                    raise AssertionError(f"broker did not start: {stderr}")
                time.sleep(0.05)

        async def scenario():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "autonomous_intelligence.mcp_server",
                    "--workspace",
                    str(workspace),
                    "--state-dir",
                    str(state),
                ],
            )
            async with Client(stdio_client(parameters)) as client:
                listed = await client.list_tools()
                assert len(listed.tools) == 5
                read = await client.call_tool(
                    "autonomous_read_file", {"path": "source.txt"}
                )
                assert read.is_error is False
                assert isinstance(read.content[0], TextContent)
                assert json.loads(read.content[0].text)["result"]["content"] == "stdio chain"

                write = await client.call_tool(
                    "autonomous_write_file",
                    {"path": "destination.txt", "content": "full MCP chain"},
                )
                assert write.is_error is False
                assert (workspace / "destination.txt").read_text() == "full MCP chain"

        asyncio.run(scenario())
    finally:
        try:
            broker_client.shutdown()
        except OSError:
            broker_process.terminate()
        broker_process.wait(timeout=5)
        if broker_process.stderr:
            broker_process.stderr.close()

    assert broker_process.returncode == 0
