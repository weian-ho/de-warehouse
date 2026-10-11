"""Command-line entry point: the only module that wires everything together.

    uv run warehouse show-config
    uv run warehouse bulk <zip>        (T025)
    uv run warehouse ingest            (T029-T030)
"""

import argparse
import logging
import sys

from pydantic import ValidationError

from warehouse.config import get_settings
from warehouse.log import new_run_id, setup_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warehouse")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show-config", help="print loaded settings (password masked)")

    bulk = sub.add_parser("bulk", help="load one downloaded Binance zip (T025)")
    bulk.add_argument("zip_path")

    ingest = sub.add_parser("ingest", help="incremental REST load (T029-T030)")
    ingest.add_argument("--symbol", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = get_settings()
    except ValidationError as exc:
        # include_input=False: never echo the values back - they may contain passwords
        print("Configuration error - check .env and config.yaml:", file=sys.stderr)
        for err in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(part) for part in err["loc"])
            print(f"  {field}: {err['msg']}", file=sys.stderr)
        return 2

    run_id = new_run_id()
    setup_logging(run_id, settings.log_level)
    logger.info("command=%s started", args.command)

    if args.command == "show-config":
        print(settings.model_dump_json(indent=2))
        print("dsn:", settings.dsn)  # prints ********** - proof the secret stays hidden
    elif args.command == "bulk":
        logger.warning("bulk not implemented yet (T025); got %s", args.zip_path)
    elif args.command == "ingest":
        logger.warning("ingest not implemented yet (T029-T030)")

    logger.info("command=%s finished", args.command)
    return 0


if __name__ == "__main__":
    sys.exit(main())