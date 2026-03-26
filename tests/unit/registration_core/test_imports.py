def test_phase1_packages_import():
    import packages.registration_core.engine  # noqa: F401
    import packages.email_providers.factory  # noqa: F401
    import packages.account_store.db  # noqa: F401
