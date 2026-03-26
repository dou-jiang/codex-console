"""Thin mailbox-provider factory for the phase 1 migration."""

from src.config.constants import EmailServiceType
from src.services import EmailServiceFactory as SourceEmailServiceFactory


class EmailProviderFactory:
    """Facade over the current mailbox factory with a phase-1-friendly API."""

    def available_types(self) -> list[str]:
        return [service_type.value for service_type in SourceEmailServiceFactory.get_available_services()]

    def create(self, service_type: str | EmailServiceType, config: dict | None = None, name: str | None = None):
        normalized = service_type if isinstance(service_type, EmailServiceType) else EmailServiceType(service_type)
        return SourceEmailServiceFactory.create(normalized, config or {}, name)


__all__ = ["EmailProviderFactory"]
