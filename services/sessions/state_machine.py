from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionStatus(str, Enum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    CLONING_REPO = "CLONING_REPO"
    RUNNING = "RUNNING"
    RECORDING = "RECORDING"
    OPENING_PR = "OPENING_PR"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.QUEUED: {SessionStatus.PROVISIONING, SessionStatus.CANCELLED},
    SessionStatus.PROVISIONING: {
        SessionStatus.CLONING_REPO,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.CLONING_REPO: {SessionStatus.RUNNING},
    SessionStatus.RUNNING: {
        SessionStatus.RECORDING,
        SessionStatus.OPENING_PR,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.RECORDING: {SessionStatus.RUNNING},
    SessionStatus.OPENING_PR: {SessionStatus.SUCCEEDED},
    SessionStatus.SUCCEEDED: set(),
    SessionStatus.FAILED: set(),
    SessionStatus.CANCELLED: set(),
}


class IllegalSessionTransitionError(RuntimeError):
    def __init__(self, current: SessionStatus, target: SessionStatus) -> None:
        super().__init__(f"illegal session transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


@dataclass(frozen=True, slots=True)
class SessionStateMachine:
    def transition(self, current: SessionStatus, target: SessionStatus) -> SessionStatus:
        if target not in TRANSITIONS[current]:
            raise IllegalSessionTransitionError(current, target)
        return target

    def can_transition(self, current: SessionStatus, target: SessionStatus) -> bool:
        return target in TRANSITIONS[current]
