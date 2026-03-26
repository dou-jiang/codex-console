from packages.email_providers.duck_mail import DuckMailService
from packages.email_providers.temp_mail import TempMailService
from packages.email_providers.tempmail import TempmailService


def test_phase1_adapter_exports_are_importable():
    assert DuckMailService is not None
    assert TempMailService is not None
    assert TempmailService is not None
