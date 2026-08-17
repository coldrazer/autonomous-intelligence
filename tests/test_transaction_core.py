from __future__ import annotations

import dataclasses
import hashlib
import threading
import uuid
from pathlib import Path

import pytest

from autonomous_intelligence.approval import ApprovalAuthority
from autonomous_intelligence.broker import ExecutionBroker
from autonomous_intelligence.engine import AutonomousEngine
from autonomous_intelligence.errors import (
    ApprovalDenied,
    InjectedCrash,
    PolicyViolation,
    ReplayConflict,
)
from autonomous_intelligence.ipc import BrokerServer, RemoteBrokerClient, default_pipe_name
from autonomous_intelligence.models import ActionClass, ActionRequest, BrokerState, EngineState


def build_system(tmp_path: Path, *, approve: bool = True, failpoint=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    state = tmp_path / "state"
    broker = ExecutionBroker(
        workspace,
        state / "broker",
        approval_provider=lambda _: approve,
        failpoint=failpoint,
    )
    engine = AutonomousEngine(state / "engine", broker)
    return workspace, state, broker, engine


def write_request(path: str, content: str, **extra) -> ActionRequest:
    params = {
        "path": path,
        "content": content,
        "expected_hash": hashlib.sha256(content.encode()).hexdigest(),
        "overwrite": False,
        **extra,
    }
    return ActionRequest.create("write_file_atomic", params)


def test_atomic_write_and_read_complete_through_broker(tmp_path: Path):
    workspace, _, broker, engine = build_system(tmp_path)
    try:
        written = engine.execute(write_request("report.txt", "safe content"))
        assert written["status"] == "VERIFIED"
        assert (workspace / "report.txt").read_text() == "safe content"

        read = engine.execute(ActionRequest.create("read_file", {"path": "report.txt"}))
        assert read["status"] == "VERIFIED"
        assert read["result"]["content"] == "safe content"
    finally:
        engine.close()
        broker.close()


def test_write_denial_is_durable_and_has_no_effect(tmp_path: Path):
    workspace, _, broker, engine = build_system(tmp_path, approve=False)
    request = write_request("denied.txt", "no")
    try:
        with pytest.raises(ApprovalDenied):
            engine.execute(request)
        assert not (workspace / "denied.txt").exists()
        assert engine.journal.get(request.attempt_id)["state"] == EngineState.REJECTED.value
        assert broker.status(request.attempt_id).state == BrokerState.REJECTED
    finally:
        engine.close()
        broker.close()


def test_workspace_escape_is_blocked_before_dispatch(tmp_path: Path):
    workspace, _, broker, engine = build_system(tmp_path)
    request = write_request(str(tmp_path / "outside.txt"), "blocked")
    try:
        with pytest.raises(PolicyViolation):
            engine.execute(request)
        assert not (tmp_path / "outside.txt").exists()
        assert broker.status(request.attempt_id) is None
        assert engine.journal.get(request.attempt_id)["state"] == EngineState.REJECTED.value
    finally:
        engine.close()
        broker.close()


def test_protected_state_path_is_blocked_even_if_nested_in_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".state"
    broker = ExecutionBroker(
        workspace,
        state / "broker",
        approval_provider=lambda _: True,
        denied_paths=[state],
    )
    engine = AutonomousEngine(state / "engine", broker)
    request = write_request(".state/attack.txt", "blocked")
    try:
        with pytest.raises(PolicyViolation):
            engine.execute(request)
        assert not (state / "attack.txt").exists()
    finally:
        engine.close()
        broker.close()


def test_duplicate_attempt_same_payload_is_idempotent(tmp_path: Path):
    workspace, _, broker, engine = build_system(tmp_path)
    (workspace / "input.txt").write_text("one")
    request = ActionRequest.create("read_file", {"path": "input.txt"})
    try:
        first = broker.submit(request)
        second = broker.submit(request)
        assert first.to_dict() == second.to_dict()
        transitions = broker.ledger._connection.execute(
            "SELECT COUNT(*) FROM broker_transitions WHERE attempt_id = ?",
            (request.attempt_id,),
        ).fetchone()[0]
        assert transitions == 3  # ACCEPTED, IN_FLIGHT, DELIVERY_ATTEMPTED
    finally:
        engine.close()
        broker.close()


def test_duplicate_completed_attempt_returns_status_after_environment_changes(tmp_path: Path):
    workspace, _, broker, engine = build_system(tmp_path)
    target = workspace / "ephemeral.txt"
    target.write_text("captured")
    request = ActionRequest.create("read_file", {"path": "ephemeral.txt"})
    try:
        first = broker.submit(request)
        target.unlink()
        second = broker.submit(request)
        assert second.to_dict() == first.to_dict()
        assert second.result["content"] == "captured"
    finally:
        engine.close()
        broker.close()


def test_duplicate_attempt_with_mutated_payload_is_rejected(tmp_path: Path):
    workspace, _, broker, engine = build_system(tmp_path)
    (workspace / "a.txt").write_text("a")
    (workspace / "b.txt").write_text("b")
    attempt_id = str(uuid.uuid4())
    logical_id = str(uuid.uuid4())
    first = ActionRequest.create(
        "read_file", {"path": "a.txt"}, logical_operation_id=logical_id, attempt_id=attempt_id
    )
    mutated = ActionRequest.create(
        "read_file", {"path": "b.txt"}, logical_operation_id=logical_id, attempt_id=attempt_id
    )
    try:
        broker.submit(first)
        with pytest.raises(ReplayConflict):
            broker.submit(mutated)
    finally:
        engine.close()
        broker.close()


def test_recovery_resumes_accepted_before_dispatch(tmp_path: Path):
    triggered = False

    def crash(label: str):
        nonlocal triggered
        if label == "after_accepted" and not triggered:
            triggered = True
            raise InjectedCrash(label)

    workspace, state, broker, engine = build_system(tmp_path, failpoint=crash)
    request = write_request("accepted.txt", "resume")
    with pytest.raises(InjectedCrash):
        engine.execute(request)
    assert broker.status(request.attempt_id).state == BrokerState.ACCEPTED
    engine.close()
    broker.close()

    broker2 = ExecutionBroker(workspace, state / "broker", approval_provider=lambda _: True)
    engine2 = AutonomousEngine(state / "engine", broker2)
    try:
        outcome = engine2.recover()[0]
        assert outcome["status"] == "VERIFIED"
        assert outcome["attempt_id"] == request.attempt_id
        assert (workspace / "accepted.txt").read_text() == "resume"
    finally:
        engine2.close()
        broker2.close()


def test_recovery_retries_with_new_attempt_when_in_flight_had_no_effect(tmp_path: Path):
    triggered = False

    def crash(label: str):
        nonlocal triggered
        if label == "after_in_flight" and not triggered:
            triggered = True
            raise InjectedCrash(label)

    workspace, state, broker, engine = build_system(tmp_path, failpoint=crash)
    request = write_request("retry.txt", "once")
    with pytest.raises(InjectedCrash):
        engine.execute(request)
    assert not (workspace / "retry.txt").exists()
    engine.close()
    broker.close()

    broker2 = ExecutionBroker(workspace, state / "broker", approval_provider=lambda _: True)
    engine2 = AutonomousEngine(state / "engine", broker2)
    try:
        outcome = engine2.recover()[0]
        attempts = engine2.journal.attempts_for(request.logical_operation_id)
        assert outcome["status"] == "VERIFIED"
        assert outcome["attempt_id"] != request.attempt_id
        states = {item["attempt_id"]: item["state"] for item in attempts}
        assert states == {
            request.attempt_id: EngineState.SUPERSEDED.value,
            outcome["attempt_id"]: EngineState.VERIFIED.value,
        }
        assert (workspace / "retry.txt").read_text() == "once"
    finally:
        engine2.close()
        broker2.close()


def test_recovery_observes_effect_without_redispatch(tmp_path: Path):
    triggered = False

    def crash(label: str):
        nonlocal triggered
        if label == "after_effect_before_receipt" and not triggered:
            triggered = True
            raise InjectedCrash(label)

    workspace, state, broker, engine = build_system(tmp_path, failpoint=crash)
    request = write_request("effect.txt", "already happened")
    with pytest.raises(InjectedCrash):
        engine.execute(request)
    assert (workspace / "effect.txt").read_text() == "already happened"
    engine.close()
    broker.close()

    broker2 = ExecutionBroker(workspace, state / "broker", approval_provider=lambda _: True)
    engine2 = AutonomousEngine(state / "engine", broker2)
    try:
        outcome = engine2.recover()[0]
        assert outcome == {
            "status": "VERIFIED",
            "attempt_id": request.attempt_id,
            "reconciled": True,
        }
        assert len(engine2.journal.attempts_for(request.logical_operation_id)) == 1
    finally:
        engine2.close()
        broker2.close()


def test_approval_token_is_single_use_and_payload_bound(tmp_path: Path):
    workspace, _, broker, engine = build_system(tmp_path)
    request = write_request("approval.txt", "bound")
    evaluated = broker.policy.evaluate(request)
    broker.ledger.accept(request, ActionClass.RECONCILABLE, evaluated.broker_payload)
    payload_hash = broker._broker_payload_hash(evaluated)
    token = broker.approvals.issue(request.attempt_id, payload_hash)
    try:
        with pytest.raises(ApprovalDenied):
            broker.approvals.consume(token, request.attempt_id, "0" * 64)
        broker.approvals.consume(token, request.attempt_id, payload_hash)
        with pytest.raises(ReplayConflict):
            broker.approvals.consume(token, request.attempt_id, payload_hash)
    finally:
        engine.close()
        broker.close()


@pytest.mark.skipif(__import__("os").name != "nt", reason="named pipe test is Windows-specific")
def test_authenticated_json_named_pipe_round_trip(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ipc.txt").write_text("over pipe")
    state = tmp_path / "state"
    broker = ExecutionBroker(workspace, state / "broker", approval_provider=lambda _: True)
    address = default_pipe_name(workspace)
    key = b"k" * 32
    server = BrokerServer(broker, address, key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = RemoteBrokerClient(address, key)
    try:
        request = ActionRequest.create("read_file", {"path": "ipc.txt"})
        receipt = client.submit(request)
        assert receipt.state == BrokerState.DELIVERY_ATTEMPTED
        assert receipt.result["content"] == "over pipe"
    finally:
        client.shutdown()
        thread.join(timeout=5)
        broker.close()
    assert not thread.is_alive()
