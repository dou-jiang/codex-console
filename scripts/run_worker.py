"""Thin wrapper script for starting the migrated worker."""

from apps.worker.main import main as worker_main


def main(argv=None) -> int:
    return worker_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
