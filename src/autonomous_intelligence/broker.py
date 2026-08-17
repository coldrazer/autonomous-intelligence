from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from .actions import SemanticActionExecutor
from .approval import ApprovalAuthority, ApprovalProvider
from .errors import ApprovalDenied, PolicyViolation, RecoveryRequired
from .models import ActionClass, ActionRequest, BrokerReceipt, BrokerState, canonical_json
from .policy import BrokerPolicy, EvaluatedAction
from .storage import BrokerLedger


Failpoint = Callable[[str], None]


def _never_crash(_: str) -> None:
    return None


class ExecutionBroker:
    """Authoritative broker for policy, approval, dispatch, and delivery state."""

    def __init__(
        self,
        workspace: str | Path,
        state_dir: str | Path,
        *,
        approval_provider: ApprovalProvider | None = None,
        failpoint: Failpoint | None = None,
        denied_paths: list[str | Path] | None = None,
    ):
        state_path = Path(state_dir)
        state_path.mkdir(parents=True, exist_ok=True)
        self.policy = BrokerPolicy(workspace, denied_paths=denied_paths)
        self.ledger = BrokerLedger(state_path / "broker.db")
        self.executor = SemanticActionExecutor()
        self.approvals = ApprovalAuthority.from_secret_file(
            state_path / "approval.key", self.ledger
        )
        self.approval_provider = approval_provider or (lambda _: False)
        self.failpoint = failpoint or _never_crash

    def close(self) -> None:
        self.ledger.close()

    def status(self, attempt_id: str) -> BrokerReceipt | None:
        return self.ledger.receipt(attempt_id)

    def submit(self, request: ActionRequest) -> BrokerReceipt:
        existing = self.ledger.get(request.attempt_id)
        if existing is not None:
            if existing.get("request_hash") and existing["request_hash"] != request.payload_hash:
                from .errors import ReplayConflict

                raise ReplayConflict("attempt_id reused with a different source request")
            row = existing
            created = False
            state = BrokerState(row["state"])
            if state != BrokerState.ACCEPTED:
                return self.ledger.receipt(request.attempt_id)  # type: ignore[return-value]
            evaluated = self._evaluated_from_row(request, row)
        else:
            evaluated = self.policy.evaluate(request)
            row, created = self.ledger.accept(
                request, evaluated.action_class, evaluated.broker_payload
            )
            state = BrokerState(row["state"])

        # Idempotent submit: identical duplicate payloads return or resume state.
        if created:
            self.failpoint("after_accepted")

        approval_id: str | None = row.get("approval_id")
        if evaluated.requires_approval and approval_id is None:
            if not self.approval_provider(evaluated.approval_summary):
                self.ledger.transition(
                    request.attempt_id,
                    BrokerState.REJECTED,
                    error="human approval denied",
                )
                raise ApprovalDenied("human approval denied")
            payload_hash = self._broker_payload_hash(evaluated)
            token = self.approvals.issue(request.attempt_id, payload_hash)
            self.approvals.consume(token, request.attempt_id, payload_hash)
            approval_id = token.approval_id

        self.ledger.transition(
            request.attempt_id,
            BrokerState.IN_FLIGHT,
            approval_id=approval_id,
        )
        self.failpoint("after_in_flight")

        try:
            result = self.executor.execute(
                evaluated.request.action, evaluated.normalized_params
            )
            self.failpoint("after_effect_before_receipt")
        except Exception as exc:
            self.ledger.transition(
                request.attempt_id,
                BrokerState.UNCERTAIN,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise RecoveryRequired(
                "dispatch raised after entering IN_FLIGHT; reconciliation is required"
            ) from exc

        self.ledger.transition(
            request.attempt_id,
            BrokerState.DELIVERY_ATTEMPTED,
            result=result,
        )
        self.failpoint("after_delivery_receipt")
        return self.ledger.receipt(request.attempt_id)  # type: ignore[return-value]

    def reconcile(self, attempt_id: str) -> dict[str, object]:
        row = self.ledger.get(attempt_id)
        if row is None:
            return {"outcome": "NOT_FOUND"}
        state = BrokerState(row["state"])
        if state == BrokerState.ACCEPTED:
            return {"outcome": "RESUME_SAFE"}
        if state not in {BrokerState.IN_FLIGHT, BrokerState.DELIVERY_ATTEMPTED}:
            return {"outcome": state.value, "error": row.get("error")}

        payload = json.loads(row["payload_json"])
        action = payload["action"]
        params = payload["params"]
        action_class = ActionClass(row["action_class"])
        if self.executor.verify(action, params):
            if state == BrokerState.IN_FLIGHT:
                self.ledger.transition(
                    attempt_id,
                    BrokerState.DELIVERY_ATTEMPTED,
                    result={"reconciled": True},
                )
            return {"outcome": "EFFECT_PRESENT", "action_class": action_class.value}

        if (
            state == BrokerState.IN_FLIGHT
            and action_class in {ActionClass.PURE, ActionClass.RECONCILABLE}
            and self.executor.precondition_allows_retry(action, params)
        ):
            self.ledger.transition(attempt_id, BrokerState.SUPERSEDED)
            return {"outcome": "RETRY_SAFE", "action_class": action_class.value}

        if state in {BrokerState.IN_FLIGHT, BrokerState.DELIVERY_ATTEMPTED}:
            self.ledger.transition(
                attempt_id,
                BrokerState.UNCERTAIN,
                error="postcondition absent and retry safety could not be proven",
            )
        return {"outcome": "UNCERTAIN", "action_class": action_class.value}

    @staticmethod
    def _evaluated_from_row(
        request: ActionRequest, row: dict[str, object]
    ) -> EvaluatedAction:
        payload = json.loads(str(row["payload_json"]))
        action_class = ActionClass(str(row["action_class"]))
        requires_approval = request.action != "read_file"
        return EvaluatedAction(
            request=request,
            action_class=action_class,
            normalized_params=payload["params"],
            requires_approval=requires_approval,
            approval_summary=f"Resume accepted {request.action} attempt {request.attempt_id}",
        )

    @staticmethod
    def _broker_payload_hash(evaluated: EvaluatedAction) -> str:
        return hashlib.sha256(
            canonical_json(evaluated.broker_payload).encode("utf-8")
        ).hexdigest()


def interactive_approval(summary: str) -> bool:
    response = input(f"Approval required: {summary}\nApprove? [y/N] ").strip().lower()
    return response in {"y", "yes"}
