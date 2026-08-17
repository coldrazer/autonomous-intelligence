from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PolicyViolation
from .models import ActionClass, ActionRequest


POLICY_VERSION = "1"


@dataclass(frozen=True)
class EvaluatedAction:
    request: ActionRequest
    action_class: ActionClass
    normalized_params: dict[str, Any]
    requires_approval: bool
    approval_summary: str

    @property
    def broker_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "policy_version": POLICY_VERSION,
            "logical_operation_id": self.request.logical_operation_id,
            "attempt_id": self.request.attempt_id,
            "action": self.request.action,
            "params": self.normalized_params,
            "action_class": self.action_class.value,
        }


class BrokerPolicy:
    """Fixed broker policy. The planner cannot choose its own action class."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        denied_paths: list[str | Path] | None = None,
    ):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise PolicyViolation("workspace must be an existing directory")
        self.denied_paths = [Path(path).resolve(strict=False) for path in denied_paths or []]

    def evaluate(self, request: ActionRequest) -> EvaluatedAction:
        handlers = {
            "read_file": self._read_file,
            "write_file_atomic": self._write_file_atomic,
        }
        handler = handlers.get(request.action)
        if handler is None:
            raise PolicyViolation(f"semantic action is not allowlisted: {request.action}")
        return handler(request)

    def _resolve_workspace_path(self, raw_path: Any, *, for_write: bool) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise PolicyViolation("path must be a non-empty string")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate

        if os.name == "nt" and ":" in candidate.name:
            raise PolicyViolation("Windows alternate data streams are prohibited")

        if for_write:
            parent = candidate.parent.resolve(strict=True)
            resolved = parent / candidate.name
        else:
            resolved = candidate.resolve(strict=True)

        try:
            common = Path(os.path.commonpath([str(self.workspace), str(resolved)]))
        except ValueError as exc:
            raise PolicyViolation("path is on a different volume") from exc
        if os.path.normcase(str(common)) != os.path.normcase(str(self.workspace)):
            raise PolicyViolation("path escapes the configured workspace")
        for denied in self.denied_paths:
            try:
                denied_common = Path(os.path.commonpath([str(denied), str(resolved)]))
            except ValueError:
                continue
            if os.path.normcase(str(denied_common)) == os.path.normcase(str(denied)):
                raise PolicyViolation("path targets protected Autonomous Intelligence state")
        return resolved

    def _read_file(self, request: ActionRequest) -> EvaluatedAction:
        path = self._resolve_workspace_path(request.params.get("path"), for_write=False)
        if not path.is_file():
            raise PolicyViolation("read_file target must be a regular file")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise PolicyViolation("read_file is limited to 10 MiB")
        max_chars = request.params.get("max_chars", 500_000)
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            raise PolicyViolation("read_file max_chars must be an integer")
        if not 1 <= max_chars <= 500_000:
            raise PolicyViolation("read_file max_chars must be between 1 and 500000")
        return EvaluatedAction(
            request=request,
            action_class=ActionClass.PURE,
            normalized_params={"path": str(path), "max_chars": max_chars},
            requires_approval=False,
            approval_summary=f"Read {path}",
        )

    def _write_file_atomic(self, request: ActionRequest) -> EvaluatedAction:
        path = self._resolve_workspace_path(request.params.get("path"), for_write=True)
        content = request.params.get("content")
        if not isinstance(content, str):
            raise PolicyViolation("write_file_atomic content must be UTF-8 text")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > 1024 * 1024:
            raise PolicyViolation("write_file_atomic is limited to 1 MiB")
        expected_hash = hashlib.sha256(content_bytes).hexdigest()
        supplied_hash = request.params.get("expected_hash")
        if supplied_hash is not None and supplied_hash != expected_hash:
            raise PolicyViolation("expected_hash does not match the supplied content")
        expected_prior_hash = request.params.get("expected_prior_hash")
        if expected_prior_hash is not None and not self._is_sha256(expected_prior_hash):
            raise PolicyViolation("expected_prior_hash must be a SHA-256 digest or null")
        overwrite = bool(request.params.get("overwrite", False))
        if path.exists() and not overwrite:
            raise PolicyViolation("target exists and overwrite was not approved")
        return EvaluatedAction(
            request=request,
            action_class=ActionClass.RECONCILABLE,
            normalized_params={
                "path": str(path),
                "content": content,
                "expected_hash": expected_hash,
                "expected_prior_hash": expected_prior_hash,
                "overwrite": overwrite,
            },
            requires_approval=True,
            approval_summary=(
                f"Atomically {'replace' if path.exists() else 'create'} {path} "
                f"with {len(content_bytes)} UTF-8 bytes (SHA-256 {expected_hash[:12]}...)"
            ),
        )

    @staticmethod
    def _is_sha256(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return True
