import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

_LOG_FORMAT = "%(asctime)s | %(name)-28s | %(levelname)-7s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# File handler is shared across all loggers (created once)
_file_handler: RotatingFileHandler | None = None


def _get_file_handler() -> RotatingFileHandler:
    global _file_handler
    if _file_handler is None:
        log_dir = Path(settings.output_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _file_handler = RotatingFileHandler(
            log_dir / "debug.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        _file_handler.setLevel(logging.DEBUG)
        _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    return _file_handler


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        # Console handler — respects the configured log level
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(settings.log_level.upper())
        console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(console_handler)

        # File handler — always DEBUG so nothing is lost on disk
        logger.addHandler(_get_file_handler())

    logger.setLevel(logging.DEBUG)
    return logger
