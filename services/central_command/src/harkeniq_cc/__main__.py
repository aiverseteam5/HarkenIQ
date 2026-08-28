"""Entry point: ``python -m harkeniq_cc`` / ``harken-cc``."""

from __future__ import annotations

import asyncio
import logging
import sys

from harkeniq_cc.config import load_cc_config
from harkeniq_cc.runtime import run


def main() -> int:
    # QA-026: structured JSON logging (R4-0 P3, finally wired). Set
    # HARKEN_LOG_PLAIN=1 for human-readable text during local debugging.
    import os

    from harkeniq.logging_config import configure_logging

    configure_logging(
        service="central-command",
        json_output=not os.environ.get("HARKEN_LOG_PLAIN"),
    )
    config = load_cc_config()
    errors = config.validate()
    if errors:
        for error in errors:
            print(f"config error: {error}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        from harkeniq_cc.license import LicenseError

        if isinstance(exc, LicenseError):
            print(f"license error: {exc}", file=sys.stderr)
            return 2
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
