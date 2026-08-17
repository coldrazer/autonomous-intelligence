from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ActionClass(StrEnum):
    PURE = "PURE"
    EXTERNALLY_IDEMPOTENT = "EXTERNALLY_IDEMPOTENT"
    RECONCILABLE = "RECONCILABLE"
    IRRECONCILABLE = "IRRECONCILABLE"


class EngineState(StrEnum):
    PREPARED = "PREPARED"
    DELIVERY_ATTEMPTED = "DELIVERY_ATTEMPTED"
    VERIFIED = "VERIFIED"
    UNCERTAIN = "UNCERTAIN"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class BrokerState(StrEnum):
    ACCEPTED = "ACCEPTED"
    IN_FLIGHT = "IN_FLIGHT"
    DELIVERY_ATTEMPTED = "DELIVERY_ATTEMPTED"
    UNCERTAIN = "UNCERTAIN"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


TERMINAL_ENGINE_STATES = {
    EngineState.VERIFIED,
    EngineState.UNCERTAIN,
    EngineState.REJECTED,
    EngineState.EXPIRED,
    EngineState.CANCELLED,
    EngineState.FAILED,
    EngineState.SUPERSEDED,
}

TERMINAL_BROKER_STATES = {
    BrokerState.UNCERTAIN,
    BrokerState.REJECTED,
    BrokerState.EXPIRED,
    BrokerState.CANCELLED,
    BrokerState.FAILED,
    BrokerState.SUPERSEDED,
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionRequest:
    logical_operation_id: str
    attempt_id: str
    action: str
    params: dict[str, Any]

    @classmethod
    def create(
        cls,
        action: str,
        params: dict[str, Any],
        *,
        logical_operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> "ActionRequest":
        return cls(
            logical_operation_id=logical_operation_id or str(uuid.uuid4()),
            attempt_id=attempt_id or str(uuid.uuid4()),
            action=action,
            params=params,
        )

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "logical_operation_id": self.logical_operation_id,
            "attempt_id": self.attempt_id,
            "action": self.action,
            "params": self.params,
        }

    @property
    def payload_hash(self) -> str:
        return sha256_json(self.canonical_payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ActionRequest":
        return cls(
            logical_operation_id=value["logical_operation_id"],
            attempt_id=value["attempt_id"],
            action=value["action"],
            params=value["params"],
        )


@dataclass(frozen=True)
class BrokerReceipt:
    attempt_id: str
    state: BrokerState
    action_class: ActionClass
    payload_hash: str
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        result["action_class"] = self.action_class.value
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BrokerReceipt":
        return cls(
            attempt_id=value["attempt_id"],
            state=BrokerState(value["state"]),
            action_class=ActionClass(value["action_class"]),
            payload_hash=value["payload_hash"],
            result=value.get("result"),
            error=value.get("error"),
        )
