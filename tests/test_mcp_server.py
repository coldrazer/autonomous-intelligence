from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client
from mcp.types import TextContent

from autonomous_intelligence.broker import ExecutionBroker
from autonomous_intelligence.mcp_server import create_mcp_application


def run(coroutine):
    return asyncio.run(coroutine)


def build_mcp(tmp_path: Path, *, approve: bool = True):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    broker = ExecutionBroker(
        workspace,
        state / "broker",
        approval_provider=lambda _: approve,
        denied_paths=[state],
    )
    application = create_mcp_application(
        workspace,
        state_dir=state,
        broker=broker,
    )
    return workspace, broker, application


def result_text(result) -> str:
    assert result.content
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text


def test_mcp_lists_small_descriptive_typed_tool_surface(tmp_path: Path):
    _, broker, application = build_mcp(tmp_path)

    async def scenario():
        async with Client(application.server) as client:
            listed = await client.list_tools()
            by_name = {tool.name: tool for tool in listed.tools}
            assert set(by_name) == {
                "autonomous_read_file",
                "autonomous_write_file",
                "autonomous_recover_incomplete",
                "autonomous_get_attempt_status",
                "autonomous_list_recent_operations",
            }
            assert all(len(tool.description or "") >= 100 for tool in listed.tools)
            assert by_name["autonomous_read_file"].input_schema["required"] == ["path"]
            assert set(by_name["autonomous_write_file"].input_schema["required"]) == {
                "path",
                "content",
            }
            assert by_name["autonomous_read_file"].annotations.read_only_hint is True
            assert by_name["autonomous_write_file"].annotations.destructive_hint is True

    try:
        run(scenario())
    finally:
        application.close()
        broker.close()


def test_mcp_read_write_status_and_recent_operations(tmp_path: Path):
    workspace, broker, application = build_mcp(tmp_path)
    (workspace / "input.txt").write_text("hello through MCP", encoding="utf-8")

    async def scenario():
        async with Client(application.server) as client:
            read_result = await client.call_tool(
                "autonomous_read_file", {"path": "input.txt", "max_chars": 5}
            )
            assert read_result.is_error is False
            read_payload = json.loads(result_text(read_result))
            assert read_payload["result"]["content"] == "hello"
            assert read_payload["result"]["truncated"] is True

            write_result = await client.call_tool(
                "autonomous_write_file",
                {"path": "output.txt", "content": "written by MCP"},
            )
            assert write_result.is_error is False
            write_payload = json.loads(result_text(write_result))
            assert write_payload["status"] == "VERIFIED"
            assert (workspace / "output.txt").read_text() == "written by MCP"

            status_result = await client.call_tool(
                "autonomous_get_attempt_status",
                {"attempt_id": write_payload["attempt_id"]},
            )
            status_payload = json.loads(result_text(status_result))
            assert status_payload["engine"]["state"] == "VERIFIED"
            assert status_payload["broker"]["state"] == "DELIVERY_ATTEMPTED"

            recent_result = await client.call_tool(
                "autonomous_list_recent_operations", {"limit": 10}
            )
            recent_payload = json.loads(result_text(recent_result))
            assert recent_payload["count"] == 2

    try:
        run(scenario())
    finally:
        application.close()
        broker.close()


def test_mcp_policy_failure_is_an_error_result_models_can_correct(tmp_path: Path):
    _, broker, application = build_mcp(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    async def scenario():
        async with Client(application.server) as client:
            result = await client.call_tool(
                "autonomous_read_file", {"path": str(outside)}
            )
            assert result.is_error is True
            text = result_text(result)
            assert "PolicyViolation" in text
            assert "inside the configured workspace" in text

    try:
        run(scenario())
    finally:
        application.close()
        broker.close()


def test_mcp_write_denial_is_reported_as_error_and_has_no_effect(tmp_path: Path):
    workspace, broker, application = build_mcp(tmp_path, approve=False)

    async def scenario():
        async with Client(application.server) as client:
            result = await client.call_tool(
                "autonomous_write_file",
                {"path": "denied.txt", "content": "must not exist"},
            )
            assert result.is_error is True
            assert "ApprovalDenied" in result_text(result)

    try:
        run(scenario())
        assert not (workspace / "denied.txt").exists()
    finally:
        application.close()
        broker.close()


def test_mcp_capabilities_resource_describes_safety_boundary(tmp_path: Path):
    workspace, broker, application = build_mcp(tmp_path)

    async def scenario():
        async with Client(application.server) as client:
            resources = await client.list_resources()
            assert [str(item.uri) for item in resources.resources] == [
                "autonomous-intelligence://capabilities"
            ]
            result = await client.read_resource("autonomous-intelligence://capabilities")
            payload = json.loads(result.contents[0].text)
            assert payload["workspace"] == str(workspace)
            assert payload["broker_separation"] is True
            assert payload["automatic_uncertain_retry"] is False

    try:
        run(scenario())
    finally:
        application.close()
        broker.close()
