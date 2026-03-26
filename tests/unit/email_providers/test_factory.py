from packages.email_providers.factory import EmailProviderFactory


def test_factory_knows_duck_and_tempmail():
    factory = EmailProviderFactory()

    assert "duck_mail" in factory.available_types()
    assert "tempmail" in factory.available_types()
