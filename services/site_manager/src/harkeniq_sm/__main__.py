"""Entry point: ``python -m harkeniq_sm`` / ``harken-sm``."""

from __future__ import annotations

import asyncio
import logging
import sys

from harkeniq_sm.config import load_sm_config
from harkeniq_sm.runtime import run


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_sm_config()
    errors = config.validate()
    if errors:
        for error in errors:
            print(f"config error: {error}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
