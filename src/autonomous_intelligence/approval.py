from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import ApprovalDenied
from .models import canonical_json
from .storage import BrokerLedger


ApprovalProvider = Callable[[str], bool]


@dataclass(frozen=True)
class ApprovalToken:
    approval_id: str
    attempt_id: str
    payload_hash: str
    expires_at: int
    signature: str

    @property
    def unsigned(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "attempt_id": self.attempt_id,
            "payload_hash": self.payload_hash,
            "expires_at": self.expires_at,
        }


class ApprovalAuthority:
    def __init__(self, secret: bytes, ledger: BrokerLedger, ttl_seconds: int = 60):
        if len(secret) < 32:
            raise ValueError("approval secret must contain at least 32 bytes")
        self.secret = secret
        self.ledger = ledger
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_secret_file(cls, path: str | Path, ledger: BrokerLedger) -> "ApprovalAuthority":
        secret_path = Path(path)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if secret_path.exists():
            secret = secret_path.read_bytes()
        else:
            secret = secrets.token_bytes(32)
            with secret_path.open("xb") as stream:
                stream.write(secret)
                stream.flush()
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass
        return cls(secret, ledger)

    def issue(self, attempt_id: str, payload_hash: str) -> ApprovalToken:
        unsigned = {
            "approval_id": str(uuid.uuid4()),
            "attempt_id": attempt_id,
            "payload_hash": payload_hash,
            "expires_at": int(time.time()) + self.ttl_seconds,
        }
        signature = hmac.new(
            self.secret,
            canonical_json(unsigned).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        token = ApprovalToken(signature=signature, **unsigned)
        self.ledger.record_approval(
            token.approval_id,
            token.attempt_id,
            token.payload_hash,
            self._token_digest(token),
            token.expires_at,
        )
        return token

    def consume(self, token: ApprovalToken, attempt_id: str, payload_hash: str) -> None:
        expected = hmac.new(
            self.secret,
            canonical_json(token.unsigned).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, token.signature):
            raise ApprovalDenied("approval signature is invalid")
        if token.attempt_id != attempt_id or token.payload_hash != payload_hash:
            raise ApprovalDenied("approval is not bound to this exact attempt and payload")
        if token.expires_at < int(time.time()):
            raise ApprovalDenied("approval has expired")
        self.ledger.consume_approval(token.approval_id, self._token_digest(token), int(time.time()))

    @staticmethod
    def _token_digest(token: ApprovalToken) -> str:
        value = {**token.unsigned, "signature": token.signature}
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
