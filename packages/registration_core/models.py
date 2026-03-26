"""Stable contracts for the phase 1 registration core."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class RegistrationInput:
    """Inputs required to execute one registration attempt."""

    email_service_type: str
    proxy_url: str | None = None
    email_service_config: dict | None = None


@dataclass(slots=True)
class AccountIdentity:
    """Minimal account identifiers captured after registration."""

    email: str = ""
    account_id: str = ""
    workspace_id: str = ""


@dataclass(slots=True)
class ExecutionLog:
    """Single structured execution log entry."""

    message: str
    level: str = "info"


@dataclass(slots=True)
class RegistrationResult:
    """Normalized result returned by the registration core."""

    success: bool
    error_message: str = ""
    identity: AccountIdentity = field(default_factory=AccountIdentity)
    logs: list[ExecutionLog] = field(default_factory=list)
