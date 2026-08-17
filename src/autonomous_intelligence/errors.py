class AutonomousIntelligenceError(Exception):
    """Base exception for Autonomous Intelligence."""


class PolicyViolation(AutonomousIntelligenceError):
    """A request is outside the broker's capability policy."""


class ReplayConflict(AutonomousIntelligenceError):
    """An attempt identifier was reused with a different payload."""


class ApprovalDenied(AutonomousIntelligenceError):
    """A required approval was denied or invalid."""


class InvalidTransition(AutonomousIntelligenceError):
    """A journal state transition violated the state machine."""


class RecoveryRequired(AutonomousIntelligenceError):
    """An operation cannot safely continue without reconciliation."""


class InjectedCrash(BaseException):
    """Test-only failpoint that behaves like abrupt process termination."""

