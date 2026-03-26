"""Thin wrapper script for starting the migrated API."""

from apps.api.main import main as api_main


def main(argv=None) -> int:
    return api_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
