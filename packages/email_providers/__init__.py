"""Mailbox adapter exports for the phase 1 migration."""

from .base import BaseEmailService, EmailServiceError, EmailServiceStatus
from .duck_mail import DuckMailService
from .factory import EmailProviderFactory
from .temp_mail import TempMailService
from .tempmail import TempmailService

__all__ = [
    "BaseEmailService",
    "DuckMailService",
    "EmailServiceError",
    "EmailServiceStatus",
    "EmailProviderFactory",
    "TempMailService",
    "TempmailService",
]
