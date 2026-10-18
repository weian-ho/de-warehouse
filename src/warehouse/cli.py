"""Command-line entry point: the only module that wires everything together.

    uv run warehouse show-config
    uv run warehouse bulk <zip> [--batch-id N] [--table schema.table]
    uv run warehouse agg-trades [--symbol S] [--from-id N] [--max-pages N]
    uv run warehouse extract [--symbol S] [--from-id N] [--max-pages N] [--stop-at-id N]
    uv run warehouse exchangeinfo
    uv run warehouse ingest            (T030)
"""

import argparse
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from warehouse.bulk import load_zip
from warehouse.client import BinanceClient
from warehouse.config import Settings, get_settings
from warehouse.errors import WarehouseError
from warehouse.extract import run_exchange_info, run_extract
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

    agg = sub.add_parser("agg-trades", help="page through REST aggTrades and log each page (no database)")
    agg.add_argument("--symbol", default=None, help="default: first symbol in config.yaml")
    agg.add_argument("--from-id", type=int, default=None, help="default: newest trade id - 2500")
    agg.add_argument("--max-pages", type=int, default=3, help="stop after this many pages")

    ext = sub.add_parser("extract", help="land aggTrades pages in bronze, starting from the watermark")
    ext.add_argument("--symbol", default=None, help="default: first symbol in config.yaml")
    ext.add_argument("--from-id", type=int, default=None, help="override the watermark and start here")
    ext.add_argument("--max-pages", type=int, default=100, help="cap on pages per run (default 100)")
    ext.add_argument("--stop-at-id", type=int, default=None, help="stop after this trade id (inclusive)")

    sub.add_parser("exchangeinfo", help="land one exchangeInfo snapshot per configured symbol in bronze")

    ingest = sub.add_parser("ingest", help="incremental REST load (T030)")
    ingest.add_argument("--symbol", default=None)
    return parser


def show_agg_trades(settings: Settings, symbol: str, from_id: int | None, max_pages: int) -> None:
    """T028 demo: fetch a few pages through the client and log them. Writes nothing."""
    with BinanceClient(settings.base_url, settings.http, settings.page_limit) as client:
        if from_id is None:
            from_id = client.latest_agg_trade_id(symbol) - 2500
            logger.info("no --from-id given; starting 2500 trades back at %s", from_id)
        for n, page in enumerate(client.iter_agg_trades(symbol, from_id), start=1):
            if n == 1:
                logger.info("first row as Binance sent it: %s", page.rows[0])
            if n >= max_pages:
                logger.info("reached --max-pages=%s; stopping (no further requests are sent)", max_pages)
                break


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
        elif args.command == "agg-trades":
            show_agg_trades(settings, args.symbol or settings.symbols[0], args.from_id, args.max_pages)
        elif args.command == "extract":
            with BinanceClient(settings.base_url, settings.http, settings.page_limit) as client:
                run_extract(
                    client,
                    settings.dsn.get_secret_value(),
                    args.symbol or settings.symbols[0],
                    run_id,
                    from_id=args.from_id,
                    max_pages=args.max_pages,
                    stop_at_id=args.stop_at_id,
                )
        elif args.command == "exchangeinfo":
            with BinanceClient(settings.base_url, settings.http, settings.page_limit) as client:
                run_exchange_info(client, settings.dsn.get_secret_value(), settings.symbols, run_id)
        elif args.command == "ingest":
            logger.warning("ingest not implemented yet (T030)")
    except WarehouseError as exc:
        logger.error("command=%s failed: %s", args.command, exc)
        return 1

    logger.info("command=%s finished", args.command)
    return 0


if __name__ == "__main__":
    sys.exit(main())