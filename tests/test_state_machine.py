from __future__ import annotations

import pytest

from services.sessions.state_machine import (
    IllegalSessionTransitionError,
    SessionStateMachine,
    SessionStatus,
)


def test_legal_transitions_pass() -> None:
    machine = SessionStateMachine()
    assert (
        machine.transition(SessionStatus.QUEUED, SessionStatus.PROVISIONING)
        == SessionStatus.PROVISIONING
    )
    assert (
        machine.transition(SessionStatus.PROVISIONING, SessionStatus.CLONING_REPO)
        == SessionStatus.CLONING_REPO
    )
    assert (
        machine.transition(SessionStatus.CLONING_REPO, SessionStatus.RUNNING)
        == SessionStatus.RUNNING
    )
    assert (
        machine.transition(SessionStatus.RUNNING, SessionStatus.RECORDING)
        == SessionStatus.RECORDING
    )
    assert (
        machine.transition(SessionStatus.RECORDING, SessionStatus.RUNNING) == SessionStatus.RUNNING
    )
    assert (
        machine.transition(SessionStatus.RUNNING, SessionStatus.OPENING_PR)
        == SessionStatus.OPENING_PR
    )
    assert (
        machine.transition(SessionStatus.OPENING_PR, SessionStatus.SUCCEEDED)
        == SessionStatus.SUCCEEDED
    )


def test_illegal_transition_raises() -> None:
    machine = SessionStateMachine()
    with pytest.raises(IllegalSessionTransitionError):
        machine.transition(SessionStatus.QUEUED, SessionStatus.RUNNING)
