def test_phase2_entrypoints_import():
    import apps.api.main  # noqa: F401
    import apps.worker.main  # noqa: F401
    import src.webui_entry  # noqa: F401
