"""Command-line entry point: the only module that wires everything together.

    uv run warehouse show-config
    uv run warehouse bulk <zip> [--batch-id N] [--table schema.table]
    uv run warehouse agg-trades [--symbol S] [--from-id N] [--max-pages N]
    uv run warehouse extract [--symbol S] [--from-id N] [--max-pages N] [--stop-at-id N]
    uv run warehouse exchangeinfo
    uv run warehouse ingest [--symbol S] [--from-id N] [--max-pages N] [--stop-at-id N]
    uv run warehouse load --run-id RUN [--symbol S] [--table schema.table] [--no-watermark]
    uv run warehouse validate [--symbol S] [--run-id RUN]
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
from warehouse.load import load_run
from warehouse.log import new_run_id, setup_logging
from warehouse.validate import FAIL, format_summary, validate_run


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

    ing = sub.add_parser("ingest", help="extract to bronze, then load that run into raw (moves the watermark)")
    ing.add_argument("--symbol", default=None, help="default: first symbol in config.yaml")
    ing.add_argument("--from-id", type=int, default=None, help="override the watermark and start here")
    ing.add_argument("--max-pages", type=int, default=100, help="cap on pages per run (default 100)")
    ing.add_argument("--stop-at-id", type=int, default=None, help="stop after this trade id (inclusive)")

    ld = sub.add_parser("load", help="re-load one run's pages from bronze into raw (no API calls)")
    ld.add_argument("--run-id", required=True, help="run_id of the extract run to load")
    ld.add_argument("--symbol", default=None, help="default: first symbol in config.yaml")
    ld.add_argument("--table", default="raw.binance_agg_trades", help="target schema.table")
    ld.add_argument("--no-watermark", action="store_true", help="side load: do not touch the watermark")
    val = sub.add_parser("validate", help="freshness, volume and schema checks for one ingest run")##we want to recognise this subcommand
    val.add_argument("--symbol", default=None, help="default: first symbol in config.yaml")##allow we specify it in the command line
    val.add_argument("--run-id", default=None, help="default: the latest aggTrades run for the symbol")
    return parser


def ingest(settings: Settings, args: argparse.Namespace, run_id: str) -> None:
    """extract (T029) then load (T030) under one run_id.

    If extract fails part-way (429s exhausted, 418, outage), the pages it already
    landed are still loaded before the error is reported, so the watermark keeps
    the progress and the next run continues from there.
    """
    symbol = args.symbol or settings.symbols[0]
    dsn = settings.dsn.get_secret_value()
    extract_error: WarehouseError | None = None
    with BinanceClient(settings.base_url, settings.http, settings.page_limit) as client:
        try:
            run_extract(client, dsn, symbol, run_id, from_id=args.from_id,
                        max_pages=args.max_pages, stop_at_id=args.stop_at_id)
        except WarehouseError as exc:
            extract_error = exc
            logger.warning("extract stopped early (%s); loading the pages that did land", exc)
    load_run(dsn, run_id, symbol)
    if extract_error is not None:
        raise extract_error


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
            ingest(settings, args, run_id)
        elif args.command == "load":
             load_run(
                settings.dsn.get_secret_value(),
                args.run_id,
                args.symbol or settings.symbols[0],
                target=args.table,
                advance_watermark=not args.no_watermark,
            )
        elif args.command == "validate":
            used_run, results = validate_run(
                settings.dsn.get_secret_value(), args.symbol or settings.symbols[0], args.run_id
            )
            print(format_summary(used_run, results))
            failed = [r.check for r in results if r.status == FAIL]
            if failed:
                logger.error("command=validate: failed checks %s", failed)
                return 1
    except WarehouseError as exc:
        logger.error("command=%s failed: %s", args.command, exc)
        return 1

    logger.info("command=%s finished", args.command)
    return 0


if __name__ == "__main__":
    sys.exit(main())