"""Logging setup. Call setup_logging() ONCE, from cli.py only.

Every other module just does:
    import logging
    logger = logging.getLogger(__name__)
"""

import logging
import sys
import uuid

FORMAT = "%(asctime)s %(levelname)-7s run=%(run_id)s %(name)s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def new_run_id() -> str:
    """Short random id that tags every log line (and later every bronze row) of one run."""
    return uuid.uuid4().hex[:8]


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging(run_id: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RunIdFilter(run_id))
    handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
