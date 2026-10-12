"""Command-line entry point: the only module that wires everything together.

    uv run warehouse show-config
    uv run warehouse bulk <zip> [--batch-id N] [--table schema.table]
    uv run warehouse ingest            (T029-T030)
"""

import argparse
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from warehouse.bulk import load_zip
from warehouse.config import get_settings
from warehouse.errors import WarehouseError
from warehouse.log import new_run_id, setup_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warehouse")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show-config", help="print loaded settings (password masked)")

    bulk = sub.add_parser("bulk", help="load one downloaded Binance aggTrades zip")
    bulk.add_argument("zip_path", type=Path)
    bulk.add_argument("--batch-id", type=int, default=1, help="batch number, as in the bash script")
    bulk.add_argument("--table", default="raw.agg_trades", help="target schema.table")
    bulk.add_argument("--stage", default="raw.agg_trades_stage", help="staging schema.table")

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

    try:
        if args.command == "show-config":
            print(settings.model_dump_json(indent=2))
            print("dsn:", settings.dsn)  # prints ********** - proof the secret stays hidden
        elif args.command == "bulk":
            load_zip(
                args.zip_path,
                settings.dsn.get_secret_value(),
                batch_id=args.batch_id,
                target=args.table,
                stage=args.stage,
            )
        elif args.command == "ingest":
            logger.warning("ingest not implemented yet (T029-T030)")
    except WarehouseError as exc:
        logger.error("command=%s failed: %s", args.command, exc)
        return 1

    logger.info("command=%s finished", args.command)
    return 0


if __name__ == "__main__":
    sys.exit(main())