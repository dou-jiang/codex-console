"""Thin wrapper script for starting the migrated API."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.api.main import main as api_main


def main(argv=None) -> int:
    return api_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
