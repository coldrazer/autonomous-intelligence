from __future__ import annotations

import argparse
import atexit
import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .cli import default_state_dir
from .engine import BrokerClient, AutonomousEngine
from .errors import AutonomousIntelligenceError
from .ipc import RemoteBrokerClient, default_pipe_name, load_or_create_ipc_key
from .models import ActionRequest, canonical_json


LOGGER = logging.getLogger(__name__)

PathArgument = Annotated[
    str,
    Field(
        min_length=1,
        max_length=4096,
        description=(
            "Path to a file inside the broker's configured workspace. Relative paths "
            "are resolved from that workspace; paths outside it are rejected."
        ),
    ),
]
ContentArgument = Annotated[
    str,
    Field(
        max_length=1024 * 1024,
        description="UTF-8 text to write. The broker enforces a 1 MiB encoded-size limit.",
    ),
]
Sha256Argument = Annotated[
    str | None,
    Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description=(
            "Optional SHA-256 digest of the file's required prior contents. Use it "
            "when overwriting to prevent replacing a file that changed after observation."
        ),
    ),
]


@dataclass
class AutonomousIntelligenceMCPApplication:
    server: MCPServer
    engine: AutonomousEngine
    broker: BrokerClient
    workspace: Path
    state_dir: Path
    _lock: threading.RLock
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            self.engine.close()
            self._closed = True


def create_mcp_application(
    workspace: str | Path,
    *,
    state_dir: str | Path | None = None,
    pipe: str | None = None,
    broker: BrokerClient | None = None,
) -> AutonomousIntelligenceMCPApplication:
    """Create the MCP adapter while keeping execution in a separate Broker."""
    resolved_workspace = Path(workspace).resolve(strict=True)
    if not resolved_workspace.is_dir():
        raise ValueError("MCP workspace must be an existing directory")
    resolved_state = (
        Path(state_dir).resolve() if state_dir is not None else default_state_dir(resolved_workspace)
    )
    resolved_state.mkdir(parents=True, exist_ok=True)

    broker_client: BrokerClient
    if broker is None:
        address = pipe or default_pipe_name(resolved_workspace)
        key = load_or_create_ipc_key(resolved_state / "ipc.key")
        broker_client = RemoteBrokerClient(address, key)
    else:
        broker_client = broker

    engine = AutonomousEngine(resolved_state / "mcp-engine", broker_client)
    server = MCPServer(
        name="autonomous-intelligence",
        title="Autonomous Intelligence",
        description=(
            "Transactional workspace tools backed by a separate policy and approval broker."
        ),
        instructions=(
            "Use read tools to inspect workspace files. Before writing, preserve the returned "
            "attempt identifiers and SHA-256 values. Writes may pause for human approval in the "
            "separate broker. Never retry an uncertain operation manually; call "
            "autonomous_recover_incomplete first."
        ),
        version=__version__,
    )
    application = AutonomousIntelligenceMCPApplication(
        server=server,
        engine=engine,
        broker=broker_client,
        workspace=resolved_workspace,
        state_dir=resolved_state,
        _lock=threading.RLock(),
    )
    _register_tools(application)
    _register_resources(application)
    return application


def _register_tools(application: AutonomousIntelligenceMCPApplication) -> None:
    server = application.server

    @server.tool(
        name="autonomous_read_file",
        title="Read workspace file",
        description=(
            "Reads one UTF-8 text file through Autonomous Intelligence's policy broker. Use this only "
            "for files inside the configured workspace. It returns content, SHA-256, operation "
            "identifiers, and truncation metadata; it cannot access arbitrary host paths."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=False,
    )
    def autonomous_read_file(
        path: PathArgument,
        max_chars: Annotated[
            int,
            Field(
                ge=1,
                le=500_000,
                description="Maximum characters returned to the model; defaults to 200000.",
            ),
        ] = 200_000,
    ) -> str:
        """Read a capability-scoped UTF-8 file and return a compact JSON string."""
        with application._lock:
            outcome = _execute(
                application,
                ActionRequest.create(
                    "read_file", {"path": path, "max_chars": max_chars}
                ),
            )
        return canonical_json(outcome)

    @server.tool(
        name="autonomous_write_file",
        title="Write workspace file atomically",
        description=(
            "Creates or atomically replaces one UTF-8 text file inside the configured workspace. "
            "The separate Broker independently validates the path and content hash and requests "
            "human approval. Use expected_prior_sha256 for safe compare-and-swap overwrites."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=False,
    )
    def autonomous_write_file(
        path: PathArgument,
        content: ContentArgument,
        overwrite: Annotated[
            bool,
            Field(
                description=(
                    "Set true only when replacing an existing file is intended. Broker approval "
                    "is still required. Defaults to false."
                )
            ),
        ] = False,
        expected_prior_sha256: Sha256Argument = None,
    ) -> str:
        """Write an exact desired file state through the transactional Broker."""
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        request = ActionRequest.create(
            "write_file_atomic",
            {
                "path": path,
                "content": content,
                "expected_hash": expected_hash,
                "expected_prior_hash": expected_prior_sha256,
                "overwrite": overwrite,
            },
        )
        with application._lock:
            outcome = _execute(application, request)
        outcome["expected_sha256"] = expected_hash
        return canonical_json(outcome)

    @server.tool(
        name="autonomous_recover_incomplete",
        title="Recover incomplete operations",
        description=(
            "Reconciles every durable incomplete Engine attempt against the authoritative Broker "
            "ledger. Call this after either process restarts or when a previous tool call ended "
            "without a result. It never blindly retries an operation whose effect is uncertain."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=False,
    )
    def autonomous_recover_incomplete() -> str:
        """Safely reconcile pending journal entries and return all recovery outcomes."""
        with application._lock:
            try:
                outcomes = application.engine.recover()
            except AutonomousIntelligenceError as exc:
                raise ValueError(_error_message(exc)) from None
            except OSError as exc:
                raise RuntimeError(f"broker_unavailable: {exc}") from None
        return canonical_json({"recovered": outcomes, "count": len(outcomes)})

    @server.tool(
        name="autonomous_get_attempt_status",
        title="Get operation attempt status",
        description=(
            "Returns the local Engine state and authoritative Broker delivery state for one exact "
            "attempt UUID. Use this to inspect an earlier write or recovery result. It performs no "
            "action and does not change retry or approval state."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=False,
    )
    def autonomous_get_attempt_status(
        attempt_id: Annotated[
            str,
            Field(
                min_length=36,
                max_length=36,
                pattern=r"^[0-9a-fA-F-]{36}$",
                description="Attempt UUID returned by another Autonomous Intelligence tool.",
            ),
        ],
    ) -> str:
        """Read status from both journals without mutating the operation."""
        with application._lock:
            engine_row = application.engine.journal.get(attempt_id)
            try:
                receipt = application.broker.status(attempt_id)
            except OSError as exc:
                raise RuntimeError(f"broker_unavailable: {exc}") from None
        if engine_row is None and receipt is None:
            raise ValueError(f"not_found: no operation attempt exists for {attempt_id}")
        return canonical_json(
            {
                "attempt_id": attempt_id,
                "engine": _public_engine_row(engine_row),
                "broker": receipt.to_dict() if receipt else None,
            }
        )

    @server.tool(
        name="autonomous_list_recent_operations",
        title="List recent operations",
        description=(
            "Lists recent Autonomous Intelligence Engine attempts with their operation IDs, action names, "
            "states, timestamps, and safe error summaries. It does not include file contents or "
            "approval secrets and performs no external action."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=False,
    )
    def autonomous_list_recent_operations(
        limit: Annotated[
            int,
            Field(ge=1, le=100, description="Maximum attempts to return, from 1 to 100."),
        ] = 20,
    ) -> str:
        """Return recent non-sensitive Engine journal summaries."""
        with application._lock:
            rows = application.engine.journal.recent(limit)
        return canonical_json({"operations": rows, "count": len(rows)})


def _register_resources(application: AutonomousIntelligenceMCPApplication) -> None:
    @application.server.resource(
        "autonomous-intelligence://capabilities",
        name="autonomous_intelligence_capabilities",
        title="Autonomous Intelligence capabilities",
        description=(
            "Static description of the workspace boundary, semantic actions, and safety behavior "
            "available through this Autonomous Intelligence MCP instance."
        ),
        mime_type="application/json",
    )
    def capabilities() -> str:
        return canonical_json(
            {
                "server": "autonomous-intelligence",
                "version": __version__,
                "workspace": str(application.workspace),
                "actions": ["read_file", "write_file_atomic"],
                "broker_separation": True,
                "write_approval_required": True,
                "automatic_uncertain_retry": False,
            }
        )


def _execute(
    application: AutonomousIntelligenceMCPApplication, request: ActionRequest
) -> dict[str, Any]:
    try:
        return application.engine.execute(request)
    except AutonomousIntelligenceError as exc:
        raise ValueError(_error_message(exc)) from None
    except (ConnectionError, OSError) as exc:
        raise RuntimeError(
            "broker_unavailable: start the Autonomous Intelligence Broker for this workspace; "
            f"the connection failed with {type(exc).__name__}: {exc}"
        ) from None
    except Exception:
        LOGGER.exception("Unexpected Autonomous Intelligence MCP tool failure")
        raise RuntimeError("internal_error: the operation failed; inspect the MCP server log") from None


def _error_message(exc: AutonomousIntelligenceError) -> str:
    category = type(exc).__name__
    suggestions = {
        "PolicyViolation": "Use a path inside the configured workspace and satisfy action limits.",
        "ApprovalDenied": "Do not retry unless the user intentionally approves the exact operation.",
        "ReplayConflict": "Create a new operation instead of reusing a mutated attempt identifier.",
        "RecoveryRequired": "Call autonomous_recover_incomplete before proposing another write.",
    }
    suggestion = suggestions.get(category, "Inspect operation status before retrying.")
    return f"{category}: {exc} Suggestion: {suggestion}"


def _public_engine_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    allowed = {
        "attempt_id",
        "logical_operation_id",
        "action",
        "state",
        "action_class",
        "error",
        "created_at",
        "updated_at",
    }
    return {key: value for key, value in row.items() if key in allowed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonomous-intelligence-mcp",
        description="Run the Autonomous Intelligence MCP v2 stdio adapter.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("AUTONOMOUS_INTELLIGENCE_WORKSPACE"),
        help="Existing capability-scoped workspace (or AUTONOMOUS_INTELLIGENCE_WORKSPACE).",
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("AUTONOMOUS_INTELLIGENCE_STATE_DIR"),
        help="State directory shared with the Broker (or AUTONOMOUS_INTELLIGENCE_STATE_DIR).",
    )
    parser.add_argument(
        "--pipe",
        default=os.environ.get("AUTONOMOUS_INTELLIGENCE_PIPE"),
        help="Broker named-pipe override (or AUTONOMOUS_INTELLIGENCE_PIPE).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.workspace:
        build_parser().error("--workspace or AUTONOMOUS_INTELLIGENCE_WORKSPACE is required")
    application = create_mcp_application(
        args.workspace,
        state_dir=args.state_dir,
        pipe=args.pipe,
    )
    atexit.register(application.close)
    application.server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
