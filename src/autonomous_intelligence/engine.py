from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Protocol

from .errors import ApprovalDenied, PolicyViolation
from .models import ActionRequest, BrokerReceipt, BrokerState, EngineState
from .storage import EngineJournal


class BrokerClient(Protocol):
    def submit(self, request: ActionRequest) -> BrokerReceipt: ...

    def status(self, attempt_id: str) -> BrokerReceipt | None: ...

    def reconcile(self, attempt_id: str) -> dict[str, object]: ...


class AutonomousEngine:
    def __init__(self, state_dir: str | Path, broker: BrokerClient):
        state_path = Path(state_dir)
        state_path.mkdir(parents=True, exist_ok=True)
        self.journal = EngineJournal(state_path / "engine.db")
        self.broker = broker

    def close(self) -> None:
        self.journal.close()

    def execute(self, request: ActionRequest) -> dict[str, object]:
        self.journal.prepare(request)
        try:
            receipt = self.broker.submit(request)
        except (ApprovalDenied, PolicyViolation) as exc:
            self.journal.transition(
                request.attempt_id,
                EngineState.REJECTED,
                error=str(exc),
            )
            raise
        return self._consume_receipt(request, receipt)

    def recover(self) -> list[dict[str, object]]:
        outcomes: list[dict[str, object]] = []
        for row in self.journal.pending():
            request = self.journal.request_for(row["attempt_id"])
            outcomes.append(self._recover_request(request))
        return outcomes

    def _recover_request(self, request: ActionRequest) -> dict[str, object]:
        receipt = self.broker.status(request.attempt_id)
        if receipt is None:
            # Broker never durably accepted it, so the same prepared attempt is safe.
            return self.execute(request)
        if receipt.state == BrokerState.ACCEPTED:
            # Accepted is pre-dispatch; idempotent submission safely resumes it.
            return self._consume_receipt(request, self.broker.submit(request))
        if receipt.state in {BrokerState.IN_FLIGHT, BrokerState.DELIVERY_ATTEMPTED}:
            reconciliation = self.broker.reconcile(request.attempt_id)
            outcome = reconciliation["outcome"]
            if outcome == "EFFECT_PRESENT":
                current = self.journal.get(request.attempt_id)
                if current and current["state"] == EngineState.PREPARED.value:
                    self.journal.transition(
                        request.attempt_id,
                        EngineState.VERIFIED,
                        action_class=receipt.action_class,
                        result={"reconciled": True},
                    )
                else:
                    self.journal.transition(
                        request.attempt_id,
                        EngineState.VERIFIED,
                        action_class=receipt.action_class,
                        result={"reconciled": True},
                    )
                return {"status": "VERIFIED", "attempt_id": request.attempt_id, "reconciled": True}
            if outcome == "RETRY_SAFE":
                self.journal.transition(request.attempt_id, EngineState.SUPERSEDED)
                retry = ActionRequest.create(
                    request.action,
                    request.params,
                    logical_operation_id=request.logical_operation_id,
                    attempt_id=str(uuid.uuid4()),
                )
                return self.execute(retry)
            self.journal.transition(
                request.attempt_id,
                EngineState.UNCERTAIN,
                action_class=receipt.action_class,
                error="broker could not prove delivery or safe retry",
            )
            return {"status": "UNCERTAIN", "attempt_id": request.attempt_id}

        target = {
            BrokerState.REJECTED: EngineState.REJECTED,
            BrokerState.EXPIRED: EngineState.EXPIRED,
            BrokerState.CANCELLED: EngineState.CANCELLED,
            BrokerState.FAILED: EngineState.FAILED,
            BrokerState.SUPERSEDED: EngineState.SUPERSEDED,
            BrokerState.UNCERTAIN: EngineState.UNCERTAIN,
        }.get(receipt.state, EngineState.UNCERTAIN)
        self.journal.transition(request.attempt_id, target, error=receipt.error)
        return {"status": target.value, "attempt_id": request.attempt_id}

    def _consume_receipt(
        self, request: ActionRequest, receipt: BrokerReceipt
    ) -> dict[str, object]:
        if receipt.state != BrokerState.DELIVERY_ATTEMPTED:
            if receipt.state == BrokerState.IN_FLIGHT:
                return self._recover_request(request)
            raise RuntimeError(f"broker returned non-delivery state: {receipt.state.value}")
        current = self.journal.get(request.attempt_id)
        if current and current["state"] == EngineState.PREPARED.value:
            self.journal.transition(
                request.attempt_id,
                EngineState.DELIVERY_ATTEMPTED,
                action_class=receipt.action_class,
                result=receipt.result,
            )
        reconciliation = self.broker.reconcile(request.attempt_id)
        if reconciliation["outcome"] != "EFFECT_PRESENT":
            self.journal.transition(
                request.attempt_id,
                EngineState.UNCERTAIN,
                action_class=receipt.action_class,
                error="postcondition verification failed after delivery",
            )
            return {"status": "UNCERTAIN", "attempt_id": request.attempt_id}
        self.journal.transition(
            request.attempt_id,
            EngineState.VERIFIED,
            action_class=receipt.action_class,
            result=receipt.result,
        )
        return {
            "status": "VERIFIED",
            "logical_operation_id": request.logical_operation_id,
            "attempt_id": request.attempt_id,
            "result": receipt.result or {},
        }
