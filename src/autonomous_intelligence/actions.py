from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import PolicyViolation


def file_sha256(path: str | Path) -> str | None:
    target = Path(path)
    if not target.is_file():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SemanticActionExecutor:
    """Executes only broker-normalized semantic actions."""

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "read_file":
            return self._read_file(params)
        if action == "write_file_atomic":
            return self._write_file_atomic(params)
        raise PolicyViolation(f"executor does not implement action: {action}")

    def verify(self, action: str, params: dict[str, Any]) -> bool:
        if action == "read_file":
            return Path(params["path"]).is_file()
        if action == "write_file_atomic":
            return file_sha256(params["path"]) == params["expected_hash"]
        return False

    def precondition_allows_retry(self, action: str, params: dict[str, Any]) -> bool:
        if action == "read_file":
            return True
        if action != "write_file_atomic":
            return False
        current_hash = file_sha256(params["path"])
        expected_prior = params.get("expected_prior_hash")
        if expected_prior is not None:
            return current_hash == expected_prior
        return current_hash is None

    @staticmethod
    def _read_file(params: dict[str, Any]) -> dict[str, Any]:
        path = Path(params["path"])
        content = path.read_text(encoding="utf-8")
        max_chars = params.get("max_chars", 500_000)
        return {
            "path": str(path),
            "content": content[:max_chars],
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "truncated": len(content) > max_chars,
            "total_characters": len(content),
        }

    @staticmethod
    def _write_file_atomic(params: dict[str, Any]) -> dict[str, Any]:
        path = Path(params["path"])
        current_hash = file_sha256(path)
        expected_prior = params.get("expected_prior_hash")
        if expected_prior is not None and current_hash != expected_prior:
            raise PolicyViolation("target changed since planning; prior hash mismatch")
        if path.exists() and not params["overwrite"]:
            raise PolicyViolation("target exists and overwrite is false")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(params["content"].encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        actual_hash = file_sha256(path)
        if actual_hash != params["expected_hash"]:
            raise OSError("atomic write completed but content verification failed")
        return {"path": str(path), "sha256": actual_hash, "bytes": len(params["content"].encode("utf-8"))}
