from __future__ import annotations

import json
import os
import secrets
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

from .broker import ExecutionBroker
from .errors import ApprovalDenied, AutonomousIntelligenceError, PolicyViolation, ReplayConflict
from .models import ActionRequest, BrokerReceipt


MAX_MESSAGE_BYTES = 12 * 1024 * 1024


def load_or_create_ipc_key(path: str | Path) -> bytes:
    key_path = Path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key = secrets.token_bytes(32)
        with key_path.open("xb") as stream:
            stream.write(key)
            stream.flush()
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
    if len(key) < 32:
        raise ValueError("IPC key must contain at least 32 bytes")
    return key


def default_pipe_name(workspace: str | Path) -> str:
    import hashlib

    digest = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:16]
    if os.name == "nt":
        return rf"\\.\pipe\autonomous_intelligence_{digest}"
    return str(Path("/tmp") / f"autonomous_intelligence_{digest}.sock")


class BrokerServer:
    """Authenticated local JSON IPC. It deliberately avoids pickle deserialization."""

    def __init__(self, broker: ExecutionBroker, address: str, authkey: bytes):
        self.broker = broker
        self.address = address
        self.authkey = authkey
        self.running = True

    def serve_forever(self) -> None:
        family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        with Listener(self.address, family=family, authkey=self.authkey) as listener:
            while self.running:
                connection = listener.accept()
                try:
                    raw = connection.recv_bytes(MAX_MESSAGE_BYTES)
                    request = json.loads(raw.decode("utf-8"))
                    response = self._handle(request)
                except Exception as exc:
                    response = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                connection.send_bytes(json.dumps(response, separators=(",", ":")).encode("utf-8"))
                connection.close()

    def _handle(self, message: dict[str, Any]) -> dict[str, Any]:
        operation = message.get("operation")
        if operation == "submit":
            request = ActionRequest.from_dict(message["request"])
            return {"ok": True, "receipt": self.broker.submit(request).to_dict()}
        if operation == "status":
            receipt = self.broker.status(message["attempt_id"])
            return {"ok": True, "receipt": receipt.to_dict() if receipt else None}
        if operation == "reconcile":
            return {"ok": True, "reconciliation": self.broker.reconcile(message["attempt_id"])}
        if operation == "shutdown":
            self.running = False
            return {"ok": True}
        raise PolicyViolation(f"unknown IPC operation: {operation}")


class RemoteBrokerClient:
    def __init__(self, address: str, authkey: bytes):
        self.address = address
        self.authkey = authkey

    def submit(self, request: ActionRequest) -> BrokerReceipt:
        response = self._call({"operation": "submit", "request": request.to_dict()})
        return BrokerReceipt.from_dict(response["receipt"])

    def status(self, attempt_id: str) -> BrokerReceipt | None:
        response = self._call({"operation": "status", "attempt_id": attempt_id})
        receipt = response["receipt"]
        return BrokerReceipt.from_dict(receipt) if receipt else None

    def reconcile(self, attempt_id: str) -> dict[str, object]:
        response = self._call({"operation": "reconcile", "attempt_id": attempt_id})
        return response["reconciliation"]

    def shutdown(self) -> None:
        self._call({"operation": "shutdown"})

    def _call(self, message: dict[str, Any]) -> dict[str, Any]:
        family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        connection = Client(self.address, family=family, authkey=self.authkey)
        try:
            payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_MESSAGE_BYTES:
                raise ValueError("IPC request exceeds message limit")
            connection.send_bytes(payload)
            response = json.loads(connection.recv_bytes(MAX_MESSAGE_BYTES).decode("utf-8"))
        finally:
            connection.close()
        if not response.get("ok"):
            error_type = response.get("error_type")
            error = response.get("error", "broker request failed")
            exception_type = {
                "ApprovalDenied": ApprovalDenied,
                "PolicyViolation": PolicyViolation,
                "ReplayConflict": ReplayConflict,
            }.get(error_type, AutonomousIntelligenceError)
            raise exception_type(error)
        return response
