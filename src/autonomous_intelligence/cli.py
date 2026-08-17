from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from .broker import ExecutionBroker, interactive_approval
from .client_config import SUPPORTED_CLIENTS, client_config_location, render_client_config
from .engine import AutonomousEngine
from .ipc import BrokerServer, RemoteBrokerClient, default_pipe_name, load_or_create_ipc_key
from .models import ActionRequest


def default_state_dir(workspace: Path) -> Path:
    digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", workspace.parent)) / "AutonomousIntelligence"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "autonomous-intelligence"
    return base / digest


def _paths(args: argparse.Namespace) -> tuple[Path, Path, str, bytes]:
    workspace = Path(args.workspace).resolve()
    state_dir = Path(args.state_dir).resolve() if args.state_dir else default_state_dir(workspace)
    address = args.pipe or default_pipe_name(workspace)
    key = load_or_create_ipc_key(state_dir / "ipc.key")
    return workspace, state_dir, address, key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonomous-intelligence",
        description="Transactional Autonomous Intelligence vertical slice",
    )
    parser.add_argument("--workspace", default=".", help="Capability-scoped workspace")
    parser.add_argument("--state-dir", help="Journal and broker state directory")
    parser.add_argument("--pipe", help="Named-pipe address override")
    subparsers = parser.add_subparsers(dest="command", required=True)

    broker = subparsers.add_parser("broker", help="Run the trusted execution broker")
    broker.add_argument(
        "--approval-mode",
        choices=["prompt", "deny", "allow"],
        default="prompt",
        help="How the broker obtains write approval",
    )

    read = subparsers.add_parser("read", help="Read a workspace file through the broker")
    read.add_argument("path")

    write = subparsers.add_parser("write", help="Atomically write a workspace file")
    write.add_argument("path")
    content = write.add_mutually_exclusive_group(required=True)
    content.add_argument("--content")
    content.add_argument("--content-file")
    write.add_argument("--overwrite", action="store_true")
    write.add_argument("--expected-prior-hash")

    subparsers.add_parser("recover", help="Recover all incomplete engine attempts")
    status = subparsers.add_parser("status", help="Show an attempt's broker status")
    status.add_argument("attempt_id")
    subparsers.add_parser("shutdown", help="Stop the broker")
    client_config = subparsers.add_parser(
        "client-config",
        help="Render a safe MCP configuration for a supported LLM client",
    )
    client_config.add_argument("client", choices=SUPPORTED_CLIENTS)
    client_config.add_argument(
        "--mcp-command",
        help="MCP executable path (defaults to the executable found on PATH)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "client-config":
        workspace = Path(args.workspace).resolve()
        mcp_command = (
            args.mcp_command
            or shutil.which("autonomous-intelligence-mcp")
            or "autonomous-intelligence-mcp"
        )
        print(f"# Target: {client_config_location(args.client)}")
        print(render_client_config(args.client, workspace, command=mcp_command), end="")
        return 0

    workspace, state_dir, address, key = _paths(args)

    if args.command == "broker":
        providers = {
            "prompt": interactive_approval,
            "deny": lambda _: False,
            "allow": lambda _: True,
        }
        broker = ExecutionBroker(
            workspace,
            state_dir / "broker",
            approval_provider=providers[args.approval_mode],
            denied_paths=[state_dir],
        )
        print(f"Broker listening on {address}")
        try:
            BrokerServer(broker, address, key).serve_forever()
        finally:
            broker.close()
        return 0

    client = RemoteBrokerClient(address, key)
    if args.command == "shutdown":
        client.shutdown()
        return 0
    if args.command == "status":
        receipt = client.status(args.attempt_id)
        print(json.dumps(receipt.to_dict() if receipt else None, indent=2))
        return 0

    engine = AutonomousEngine(state_dir / "engine", client)
    try:
        if args.command == "recover":
            print(json.dumps(engine.recover(), indent=2))
            return 0
        if args.command == "read":
            request = ActionRequest.create("read_file", {"path": args.path})
        elif args.command == "write":
            text = (
                Path(args.content_file).read_text(encoding="utf-8")
                if args.content_file
                else args.content
            )
            request = ActionRequest.create(
                "write_file_atomic",
                {
                    "path": args.path,
                    "content": text,
                    "expected_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "expected_prior_hash": args.expected_prior_hash,
                    "overwrite": args.overwrite,
                },
            )
        else:
            raise AssertionError(args.command)
        print(json.dumps(engine.execute(request), indent=2))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    sys.exit(main())
