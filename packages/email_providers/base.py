"""Thin mailbox-provider base exports for the phase 1 migration."""

from src.services.base import BaseEmailService, EmailServiceError, EmailServiceStatus

__all__ = ["BaseEmailService", "EmailServiceError", "EmailServiceStatus"]
