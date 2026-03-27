"""Thin compatibility wrapper for direct repository execution."""

from src.webui_entry import main, setup_application, start_webui

__all__ = ["main", "setup_application", "start_webui"]


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
