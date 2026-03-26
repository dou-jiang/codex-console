"""Registration-specific exceptions for the phase 1 core."""


class RegistrationError(Exception):
    """Base exception for registration core failures."""


class MailboxProviderError(RegistrationError):
    """Raised when mailbox provisioning or OTP retrieval fails."""


class RegistrationClientError(RegistrationError):
    """Raised when the OpenAI-facing client boundary fails."""
